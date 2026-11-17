"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const repo = path.resolve(__dirname, "..", "..");
const html = fs.readFileSync(path.join(repo, "frontend", "static", "index.html"), "utf8");
const js = fs.readFileSync(path.join(repo, "frontend", "static", "app.js"), "utf8");
const css = fs.readFileSync(path.join(repo, "frontend", "static", "style.css"), "utf8");
const tokens = fs.readFileSync(path.join(repo, "frontend", "static", "_tokens.css"), "utf8");
const primitives = fs.readFileSync(path.join(repo, "frontend", "static", "_primitives.css"), "utf8");
const pipelineHtml = fs.readFileSync(path.join(repo, "frontend", "static", "pipeline.html"), "utf8");
const pipelineCss = fs.readFileSync(path.join(repo, "frontend", "static", "pipeline.css"), "utf8");
const pipelineJs = fs.readFileSync(path.join(repo, "frontend", "static", "pipeline.js"), "utf8");
const liquidGlassCss = fs.readFileSync(path.join(repo, "frontend", "static", "liquid_glass.css"), "utf8");
const liquidGlassJs = fs.readFileSync(path.join(repo, "frontend", "static", "liquid_glass.js"), "utf8");
const completionHtml = fs.readFileSync(path.join(repo, "frontend", "static", "completion.html"), "utf8");
const completionCss = fs.readFileSync(path.join(repo, "frontend", "static", "completion.css"), "utf8");
const loadingHtml = fs.readFileSync(path.join(repo, "client", "loading.html"), "utf8");
const loadingCss = fs.readFileSync(path.join(repo, "client", "loading.css"), "utf8");
const icons = fs.readFileSync(path.join(repo, "frontend", "static", "icons.svg"), "utf8");
const server = fs.readFileSync(path.join(repo, "frontend", "server.py"), "utf8");
const slashInput = require(path.join(repo, "frontend", "static", "slash_input.js"));

test("composer exposes attachment selection, drop, paste and history cards", () => {
  assert.match(html, /id="attach-btn"/);
  assert.match(html, /id="attachment-tray"/);
  assert.match(js, /uploadAttachments/);
  assert.match(js, /addEventListener\("drop"/);
  assert.match(js, /addEventListener\("paste"/);
  assert.match(js, /renderMessageAttachments/);
});

test("expert setup uses a bounded dialog and atomic activation", () => {
  assert.match(html, /id="settings-overlay"/);
  assert.match(js, /activate,/);
  assert.match(js, /保存并启用专家团/);
  assert.match(js, /createRouteEditor/);
  assert.doesNotMatch(js, /expert-config-popover/);
  assert.match(css, /max-height:\s*calc\(100dvh - 48px\)/);
});

test("expert controls stay grouped and creative actions come from capabilities", () => {
  assert.match(html, /class="expert-control-group"[\s\S]*id="expert-mode-btn"[\s\S]*id="expert-config-btn"/);
  assert.match(css, /\.expert-control-group\s*\{[\s\S]*display:\s*inline-flex/);
  assert.match(pipelineHtml, /data-palette-layer="conversation"[\s\S]*data-palette-layer="tool"[\s\S]*data-palette-layer="control"/);
  assert.match(pipelineHtml, /id="conversation-action-palette"/);
  assert.match(pipelineHtml, /id="tool-action-palette"/);
  assert.match(pipelineHtml, /id="workflow-overview"/);
  assert.match(pipelineJs, /Array\.isArray\(caps\.actions\)/);
  assert.match(pipelineJs, /function workflowLayer/);
  assert.match(pipelineJs, /rc-layer-group/);
  assert.match(pipelineJs, /data-action-param/);
  assert.match(pipelineJs, /conversation_turn_completed/);
  assert.match(pipelineCss, /\.ncard\.node-action/);
});

test("expert timeline is automatic, resumable and keeps detailed telemetry", () => {
  assert.doesNotMatch(js, /expert_strategy_response/);
  assert.doesNotMatch(server, /expert_strategy_response/);
  assert.match(js, /setInterval\([\s\S]*100\)/);
  assert.match(js, /expert_tool_result/);
  assert.match(js, /expert_thinking/);
  assert.match(js, /expert_usage/);
  assert.match(js, /expert_child_event/);
  assert.match(js, /type:\s*"expert_resume"/);
  assert.match(js, /type:\s*"expert_terminate"/);
  assert.match(server, /mtype == "expert_resume"/);
  assert.match(server, /mtype == "expert_terminate"/);
  assert.match(css, /expert-run-controls/);
});

test("top bar is an IDE style draggable shell", () => {
  assert.match(html, /id="topbar-project"/);
  assert.match(html, /id="topbar-conversation"/);
  assert.match(css, /-webkit-app-region:\s*drag/);
  assert.match(css, /titlebar-area-width/);
  assert.match(css, /--window-controls-width/);
});

test("sidebar brand is a restrained MewCode monogram", () => {
  assert.match(html, /<div class="logo-area">[\s\S]*<div class="logo-monogram"[^>]*aria-hidden="true">M<\/div>/);
  assert.match(html, /<div class="logo-name">MewCode<\/div>/);
  assert.match(html, /<div class="logo-sub">LOCAL AGENT WORKSPACE<\/div>/);
  assert.doesNotMatch(html, /logo-cat/);
  assert.match(css, /\.logo-area\s*\{[\s\S]*border-bottom/);
  assert.match(css, /\.logo-monogram\s*\{[\s\S]*width:\s*32px[\s\S]*height:\s*32px[\s\S]*border-radius:/);
  assert.match(css, /\.logo-monogram\s*\{[\s\S]*background:\s*var\(--glass-clear\)/);
  assert.doesNotMatch(css.slice(css.indexOf(".logo-area"), css.indexOf(".search-box")), /gradient\(/i);
});

test("conversation navigation remains available while other chats stream", () => {
  assert.match(js, /runningConversations\s*=\s*new Set/);
  assert.match(js, /conversation_id:\s*conversationId/);
  assert.match(js, /activateConversation\(data\.id,\s*!!data\.running\)/);
  assert.match(js, /c\.running\s*\?\s*" running"/);
  assert.doesNotMatch(js, /正在响应中，请先打断或等待完成再新建对话/);
  assert.doesNotMatch(js, /正在响应中，请先打断或等待完成再切换对话/);
});

test("conversation navigation does not wait for read-state bookkeeping", () => {
  const openWorkspace = js.slice(
    js.indexOf("async function openWorkspaceConversation"),
    js.indexOf("async function openExternalConversation"),
  );
  assert.match(openWorkspace, /void patchSidebarConversationState\("mark_read", c\)/);
  assert.doesNotMatch(openWorkspace, /await patchSidebarConversationState\("mark_read", c\)/);
  assert.match(openWorkspace, /return openConversation/);
});

test("conversation navigation locks sends and ignores stale completion", () => {
  assert.match(js, /let conversationNavigationToken = 0/);
  assert.match(js, /let conversationNavigationPending = false/);
  assert.match(js, /function beginConversationNavigation\(\)/);
  assert.match(js, /function endConversationNavigation\(token\)/);
  assert.match(js, /sendBtn\.disabled = conversationNavigationPending \|\| streaming \|\| !connected/);

  const sendChat = js.slice(js.indexOf("async function sendChat"), js.indexOf("sendBtn.addEventListener"));
  assert.match(sendChat, /if \(conversationNavigationPending\) return/);

  const openConversation = js.slice(
    js.indexOf("async function openConversation"),
    js.indexOf("function renderEntries"),
  );
  assert.match(openConversation, /const navigationToken = beginConversationNavigation\(\)/);
  assert.match(openConversation, /navigationToken !== conversationNavigationToken/);
  assert.match(openConversation, /refreshState\(navigationToken\)/);
  assert.match(openConversation, /endConversationNavigation\(navigationToken\)/);

  const openForeign = js.slice(
    js.indexOf("async function openForeignConversation"),
    js.indexOf("function renderEntries"),
  );
  assert.match(openForeign, /const navigationToken = beginConversationNavigation\(\)/);
  assert.match(openForeign, /await openConversation\("project", id\)/);
});

test("live chat renderer preserves text and tool chronology", () => {
  assert.match(js, /let currentText = "";\s*let currentTextSegment = "";\s*let currentMarkdownSegment = null;/);

  const appendDelta = js.slice(js.indexOf("function appendDelta"), js.indexOf("function appendThinking"));
  assert.match(appendDelta, /if \(!currentMarkdownSegment\) currentToolGroup = null/);
  assert.match(appendDelta, /const el = ensureMarkdownSegment\(\)/);
  assert.match(appendDelta, /currentText \+= chunk/);
  assert.match(appendDelta, /currentTextSegment \+= chunk/);
  assert.match(appendDelta, /renderMarkdown\(currentTextSegment\)/);

  const renderTool = js.slice(js.indexOf("function renderTool"), js.indexOf("function renderToolResult"));
  assert.match(renderTool, /closeMarkdownSegment\(\)/);
  assert.match(renderTool, /if \(!currentToolGroup\)/);
  assert.match(renderTool, /currentToolGroup\.querySelector\("\.tg-body"\)\.appendChild\(row\)/);

  const replay = js.slice(js.indexOf("function replayConversationEvents"), js.indexOf("// ---------- 工具函数 ----------"));
  assert.match(replay, /appendDelta\(message\.text\)/);
  assert.match(replay, /renderTool\(message\)/);
  assert.match(replay, /renderToolResult\(message\)/);

  const finish = js.slice(js.indexOf("function finishStream"), js.indexOf("// ---------- 输入与发送 ----------"));
  assert.match(finish, /closeMarkdownSegment\(/);
  assert.match(finish, /enhanceMath\(/);
  assert.match(finish, /clearLiveMarkdownState\(true\)/);
  assert.match(finish, /currentMarkdownSegment = null/);
  assert.match(finish, /currentTextSegment = ""/);

  assert.match(html, /style\.css\?v=model-routing-1/);
  assert.match(html, /app\.js\?v=node-timeline-51/);
});

test("project entries expose a guarded keyboard-accessible context menu", () => {
  assert.match(js, /addEventListener\("contextmenu"/);
  assert.match(js, /event\.shiftKey && event\.key === "F10"/);
  assert.match(js, /setAttribute\("role", "menu"\)/);
  assert.match(js, /setAttribute\("role", "menuitem"\)/);
  assert.match(js, /在资源管理器中打开/);
  assert.match(js, /if \(!project\.active\)/);
  assert.match(js, /仅从项目列表中移除，不会删除本地目录、文件或历史会话/);
  assert.match(js, /fetch\("\/api\/projects\/remove"/);
  assert.match(js, /typeof desktop\.openDirectory === "function"/);
  assert.match(js, /projectContextReturnFocus/);
  assert.match(css, /#popover\.project-context-menu/);
  assert.match(html, /style\.css\?v=model-routing-1/);
  assert.match(html, /app\.js\?v=node-timeline-51/);
});

test("slash keyboard controller handles IME, arrows, tab, escape and enter", () => {
  const input = new EventTarget();
  let selectedIndex = 0;
  let selected = "";
  let submitted = 0;
  let hidden = 0;
  let rendered = 0;
  let value = "/p";
  const matches = [
    { name: "plan", aliases: ["p"] },
    { name: "goal", aliases: ["g"] },
  ];
  slashInput.bind(input, () => ({
    value,
    menuOpen: hidden === 0,
    matches,
    selectedIndex,
    setSelectedIndex: (index) => { selectedIndex = index; },
    render: () => { rendered += 1; },
    hide: () => { hidden += 1; },
    select: (command) => { selected = command.name; },
    submit: () => { submitted += 1; },
  }));
  const key = (name, options = {}) => {
    const event = new Event("keydown", { cancelable: true });
    Object.defineProperties(event, {
      key: { value: name },
      keyCode: { value: options.keyCode || 0 },
      isComposing: { value: !!options.isComposing },
      shiftKey: { value: !!options.shiftKey },
    });
    input.dispatchEvent(event);
    return event;
  };

  key("Enter", { isComposing: true, keyCode: 229 });
  assert.equal(submitted, 0, "IME composition must not submit");
  key("ArrowDown");
  assert.equal(selectedIndex, 1);
  assert.equal(rendered, 1);
  key("Tab");
  assert.equal(selected, "goal");
  selectedIndex = 0;
  value = "/plan";
  key("Enter");
  assert.equal(submitted, 1, "exact slash command submits on Enter");
  hidden = 0;
  key("Escape");
  assert.equal(hidden, 1);
});

test("composer exposes and dispatches slash commands", () => {
  assert.match(html, /id="slash-menu"/);
  assert.match(js, /function renderSlashMenu\(\)/);
  assert.match(js, /function parseSlashCommand\(text\)/);
  assert.match(js, /type:\s*"command"/);
  assert.match(js, /case "command_result"/);
  assert.match(js, /let slashCommands = \[\]/);
  assert.match(js, /slashCommands = msg\.commands/);
  assert.match(css, /\.slash-command\.is-selected/);
});

test("runtime model picker is a persistent three-column IQ workspace", () => {
  assert.match(js, /runtime-settings-grid/);
  assert.match(js, /1\. Provider/);
  assert.match(js, /2\. 模型/);
  assert.match(js, /3\. Effort/);
  assert.match(js, /点击切换到默认模型/);
  assert.match(js, /type:\s*"select_model_ref"/);
  assert.match(js, /type:\s*"set_effort"/);
  assert.match(js, /runtime-advanced/);
  assert.match(js, /Radar 数据时间/);
  assert.match(js, /官方能力目录/);
  assert.match(js, /context_policy/);
  assert.match(js, /modelContextBadge/);
  assert.match(js, /最后检查/);
  assert.match(js, /已刷新，Radar 暂无新数据/);
  assert.match(js, /cache:\s*"no-store"/);
  assert.match(js, /refresh=true&_=/);
  assert.match(css, /#popover\.runtime-model-popover[\s\S]*840px/);
  assert.match(css, /\.runtime-settings-grid[\s\S]*grid-template-columns:\s*minmax/);
  assert.match(css, /@media \(max-width:\s*720px\)[\s\S]*\.runtime-settings-grid[\s\S]*grid-template-columns:\s*1fr/);
  assert.match(server, /JSONResponse\(catalog, headers=\{"Cache-Control": "no-store"\}\)/);
  assert.match(server, /catalog\["capability_catalog"\] = capability_catalog/);
});

test("runtime model picker opens before the remote IQ catalog resolves", () => {
  const picker = js.slice(js.indexOf("function showRuntimeSettings"));
  assert.doesNotMatch(picker, /async function showRuntimeSettings/);
  assert.doesNotMatch(picker, /await loadModelCatalog\(\)/);
  assert.match(picker, /showPopover\(anchor/);
  assert.match(picker, /void refreshRuntimeSettings\(state, false, true\)/);
  assert.match(js, /let modelCatalogRequest = null/);
  assert.match(js, /正在加载 Provider 与 IQ 数据/);
});

test("cache badge distinguishes unreported telemetry from an explicit zero", () => {
  assert.match(js, /cache_cumulative_usage_available/);
  assert.match(js, /cache_request_usage_available/);
  assert.match(js, /cache\.requestAvailable \? formatCacheRate/);
  assert.match(js, /cache e" \+ cache\.epoch \+ " 未上报/);
  assert.doesNotMatch(js, /cacheCreation > 0 \|\|\s*epoch > 0/);
});

test("all renderer workspaces share the porcelain glass tokens and primitives", () => {
  for (const page of [html, pipelineHtml, completionHtml]) {
    assert.match(page, /_tokens\.css\?v=porcelain-glass-3/);
    assert.match(page, /_primitives\.css\?v=porcelain-glass-3/);
  }
  assert.match(tokens, /--radius-xs:\s*4px/);
  assert.match(tokens, /--radius-xl:\s*20px/);
  assert.match(tokens, /--glass-modal:/);
  assert.match(primitives, /\.material-modal/);
  assert.match(primitives, /prefers-reduced-motion/);
  assert.match(primitives, /forced-colors:\s*active/);
});

test("chat renderer keeps a readable Claude-style content hierarchy", () => {
  assert.match(tokens, /--font-body:\s*15px/);
  assert.match(tokens, /--font-display:\s*30px/);
  assert.match(css, /\.chat-empty-state h1[\s\S]*font-size:\s*var\(--font-display\)/);
  assert.match(css, /\.msg\.assistant \.md[\s\S]*font-size:\s*var\(--font-body\)/);
  assert.match(css, /\.composer-shell[\s\S]*max-width:\s*var\(--composer-width\)/);
  assert.match(html, /告诉 MewCode 你想完成什么/);
  assert.match(js, /今天想完成什么/);
  assert.match(js, /规划复杂任务/);
});

test("styles avoid broad transitions and keep glass on elevated surfaces", () => {
  for (const source of [css, pipelineCss, completionCss, primitives, loadingCss]) {
    assert.doesNotMatch(source, /transition:\s*all\b/i);
  }
  assert.match(pipelineCss, /\.canvas[\s\S]*radial-gradient/);
  assert.match(pipelineCss, /\.ncard[\s\S]*background:\s*var\(--glass-bg\)/);
  assert.match(completionCss, /body[\s\S]*radial-gradient/);
  assert.doesNotMatch(completionCss, /linear-gradient\(var\(--grid-line\)/);
});

test("fixed dialogs share an accessible liquid-glass optical layer", () => {
  assert.match(html, /style\.css\?v=model-routing-1[\s\S]*liquid_glass\.css\?v=liquid-glass-9/);
  assert.match(pipelineHtml, /pipeline\.css\?v=workflow-layers-1[\s\S]*liquid_glass\.css\?v=liquid-glass-6/);
  assert.match(html, /liquid_glass\.js\?v=liquid-glass-9[\s\S]*<\/body>/);
  assert.match(pipelineHtml, /liquid_glass\.js\?v=liquid-glass-6[\s\S]*<\/body>/);

  for (const selector of ["settings-dialog", "mew-confirm", "modal"]) {
    assert.match(liquidGlassCss, new RegExp(`\\.${selector}[\\s\\S]*backdrop-filter`));
    assert.match(liquidGlassCss, new RegExp(`\\.${selector}::before`));
    assert.match(liquidGlassCss, new RegExp(`\\.${selector}::after`));
  }
  assert.match(liquidGlassCss, /backdrop-filter:\s*blur\([^;]+saturate\([^;]+contrast\(/);
  assert.match(liquidGlassCss, /radial-gradient/);
  assert.match(liquidGlassCss, /linear-gradient/);
  assert.match(liquidGlassCss, /inset\s+0\s+1px/);
  assert.match(liquidGlassCss, /@supports not \(\(backdrop-filter:/);
  assert.match(liquidGlassCss, /prefers-contrast:\s*more/);
  assert.match(liquidGlassCss, /forced-colors:\s*active/);
  assert.match(liquidGlassCss, /prefers-reduced-motion:\s*reduce/);
  const composerStart = liquidGlassCss.indexOf(".input-wrap.has-liquid-composer {");
  const composerEnd = liquidGlassCss.length;
  assert.ok(composerStart >= 0, "Composer enhancement rules should exist");
  const composerCss = liquidGlassCss.slice(composerStart, composerEnd);
  const composerRule = composerCss.match(/\.input-wrap\.has-liquid-composer\s*\{([\s\S]*?)\}/);
  const opticsRule = composerCss.match(/\.input-wrap\.has-liquid-composer\s*>\s*\.liquid-composer-optics\s*\{([\s\S]*?)\}/);
  assert.ok(composerRule, "Composer panel rule should be extractable");
  assert.ok(opticsRule, "Composer optics rule should be extractable");
  assert.doesNotMatch(composerRule[1], /gradient\(/i);
  assert.doesNotMatch(opticsRule[1], /gradient\(/i);
  assert.match(composerRule[1], /background:\s*var\(--liquid-composer-panel\)/);
  assert.match(composerRule[1], /backdrop-filter:\s*blur\(/);
  assert.match(opticsRule[1], /background:\s*transparent/);
  assert.match(opticsRule[1], /backdrop-filter:\s*url\("#mew-liquid-composer-refraction"\)/);
  assert.match(composerCss, /\.input-wrap\.has-liquid-composer\s*>\s*\.liquid-composer-optics::before[\s\S]*border:\s*1px\s+solid\s+var\(--liquid-composer-edge\)/);
  assert.match(composerCss, /\.input-wrap\.has-liquid-composer\s*>\s*\.liquid-composer-optics::after[\s\S]*display:\s*none/);
  assert.doesNotMatch(composerCss, /liquid-composer-(?:edge-)?(?:cool|warm)/i);
  assert.doesNotMatch(composerCss, /mask(?:-composite)?\s*:/i);
  assert.doesNotMatch(composerCss, /chromatic|caustic/i);
  assert.match(liquidGlassCss, /\.liquid-composer-optics[\s\S]*backdrop-filter:\s*url\("#mew-liquid-composer-refraction"\)/);
  assert.match(liquidGlassCss, /@supports not \(\(backdrop-filter:\s*url\("#mew-liquid-composer-refraction"\)/);
  assert.match(liquidGlassCss, /@supports not \(\(filter:\s*url\("#mew-liquid-composer-refraction"\)/);
  assert.match(liquidGlassCss, /\.input-wrap\.has-liquid-composer[\s\S]*z-index:\s*0/);
  assert.match(liquidGlassCss, /\.input-wrap\.has-liquid-composer[\s\S]*pointer-events:\s*none/);
  assert.match(liquidGlassCss, /:not\(\.liquid-composer-optics\):not\(\.liquid-composer-svg\)[\s\S]*z-index:\s*1/);
  assert.match(liquidGlassCss, /\.is-dragging::after[\s\S]*z-index:\s*5/);
  assert.doesNotMatch(liquidGlassCss, /transition:\s*all\b/i);

  assert.match(liquidGlassJs, /COMPOSER_SELECTOR\s*=\s*"\.input-wrap"/);
  assert.match(liquidGlassJs, /FILTER_ID\s*=\s*"mew-liquid-composer-refraction"/);
  for (const primitive of ["feTurbulence", "feGaussianBlur", "feDisplacementMap", "feSpecularLighting", "fePointLight", "feComposite", "feBlend"]) {
    assert.match(liquidGlassJs, new RegExp(`createSvgNode\\("${primitive}"`));
  }
  assert.match(liquidGlassJs, /ensureComposerOptics/);
  assert.match(liquidGlassJs, /COMPOSER_OPTICS_CLASS/);
  assert.match(liquidGlassJs, /createSvgNode\("svg"\)/);
  assert.match(liquidGlassJs, /setAttribute\("class",\s*COMPOSER_SVG_CLASS\)/);
  assert.match(liquidGlassJs, /setAttribute\("aria-hidden",\s*"true"\)/);
  assert.match(liquidGlassJs, /composers\.length === 1/);
  assert.match(liquidGlassJs, /typeof document === "undefined"/);
  assert.match(liquidGlassJs, /focusin/);
  assert.match(liquidGlassJs, /focusout/);
  assert.match(liquidGlassJs, /composer\.contains\(document\.activeElement\)/);
  assert.match(liquidGlassJs, /classList\.toggle\(COMPOSER_ACTIVE_CLASS/);
  assert.match(liquidGlassJs, /pointermove/);
  assert.match(liquidGlassJs, /pointerleave/);
  assert.match(liquidGlassJs, /requestAnimationFrame/);
  assert.match(liquidGlassJs, /MutationObserver/);
  assert.match(liquidGlassJs, /prefers-reduced-motion/);
  assert.match(liquidGlassJs, /setProperty\(POINTER_X/);
  assert.match(liquidGlassJs, /removeProperty\(POINTER_X/);
  assert.match(liquidGlassJs, /specularConstant:\s*"0\.045"/);
  assert.match(liquidGlassJs, /specularExponent:\s*"(?:4[5-9]|5[0-9]|6[0-9])"/);
});

test("composer uses container queries and empty prompts only fill the input", () => {
  assert.match(html, /class="composer-shell"/);
  assert.match(css, /container:\s*composer\s*\/\s*inline-size/);
  assert.match(css, /@container composer \(max-width:\s*650px\)/);
  const emptyState = js.slice(js.indexOf("function renderEmptyState"), js.indexOf("function applyTheme"));
  assert.match(emptyState, /inputEl\.value\s*=\s*prompt/);
  assert.match(emptyState, /inputEl\.focus\(\)/);
  assert.doesNotMatch(emptyState, /sendMessage|sendChat|\.click\(\)/);
});

test("local SVG sprite replaces major shell glyphs", () => {
  for (const symbol of ["menu", "send", "settings", "agents", "expert", "completion", "close"]) {
    assert.match(icons, new RegExp(`id="${symbol}"`));
  }
  assert.match(html, /icons\.svg#send/);
  assert.match(pipelineHtml, /icons\.svg#play/);
  assert.match(completionHtml, /icons\.svg#completion/);
  assert.doesNotMatch(pipelineHtml, /[▶■◷▤⛶⌫−×✕⚙]/);
  const symbolIds = new Set([...icons.matchAll(/<symbol id="([^"]+)"/g)].map((match) => match[1]));
  for (const source of [html, js, pipelineHtml, completionHtml]) {
    for (const match of source.matchAll(/icons\.svg#([a-z0-9-]+)/g)) {
      assert.ok(symbolIds.has(match[1]), `missing SVG symbol: ${match[1]}`);
    }
  }
  assert.match(pipelineCss, /@media \(max-width:\s*1100px\)[\s\S]*\.topbar-right \.btn > span \{ display: none; \}/);
});

test("Electron loading screen uses the porcelain glass hierarchy", () => {
  assert.match(loadingHtml, /id="progress"/);
  assert.match(loadingHtml, /id="error-panel"/);
  assert.match(loadingCss, /-webkit-app-region:\s*drag/);
  assert.match(loadingCss, /@media \(prefers-reduced-motion:\s*reduce\)/);
});

test("provider connection feedback stays below the desktop window controls", () => {
  assert.match(js, /provider-test-feedback/);
  assert.match(js, /showTestFeedback/);
  assert.match(js, /context_channel:\s*contextChannel\.value/);
  assert.match(primitives, /\.is-desktop #mew-feedback-toasts[\s\S]*titlebar-area-height/);

  const providerManager = js.slice(
    js.indexOf("async function openProviderManager"),
    js.indexOf("function renderSidebarSkeleton"),
  );
  assert.doesNotMatch(providerManager, /notify\(result\.ok/);
});

test("background tasks drawer always provides a route back to the conversation", () => {
  assert.match(html, /id="bgtasks-backdrop"/);
  assert.match(html, /aria-controls="bgtasks-panel"/);
  assert.match(js, /function setBgTasksOpen\(open/);
  assert.match(js, /btBackdrop\.addEventListener\("click"/);
  assert.match(js, /event\.key === "Escape"/);
  assert.match(css, /\.is-desktop #bgtasks-panel,[\s\S]*top:\s*52px/);
  assert.match(html, /style\.css\?v=model-routing-1/);
  assert.match(html, /app\.js\?v=node-timeline-51/);
});

test("official OpenRouter provider form pulls an account catalog instead of a manual list", () => {
  assert.match(js, /isOfficialOpenRouter = \(\) =>/);
  assert.match(js, /请先测试连接以拉取账号模型列表/);
  assert.match(js, /请选择默认模型/);
  assert.match(js, /已验证账号模型目录/);
  assert.match(js, /openrouterModels/);
  assert.match(js, /手工模型列表/);
});

test("saved providers can be edited and tested without re-entering their secret", () => {
  assert.match(js, /editProviderProfile\(\(profiles \|\| \[\]\)\[0\] \|\| null\)/);
  assert.match(js, /provider_key:\s*editingProviderKey \|\| undefined/);
  assert.match(js, /留空则继续使用已保存密钥/);
  assert.match(js, /testProviderProfile\(profile, testSaved\)/);
  assert.match(primitives, /top:\s*max\(64px/);
});

test("provider connection tests use the same-origin backend instead of short IPC", () => {
  const helper = js.slice(
    js.indexOf("async function testProviderConnection"),
    js.indexOf("const GROK_DESKTOP_PROVIDER_PRESET"),
  );
  assert.match(helper, /fetch\("\/api\/client\/providers\/test"/);
  assert.match(helper, /credentials:\s*"same-origin"/);
  assert.doesNotMatch(helper, /desktop\.testProvider/);
  assert.match(helper, /result\.error/);
  assert.match(helper, /Provider 测试未通过桌面会话校验/);
});

test("clicking a provider switches directly to its configured default model", () => {
  assert.match(js, /function beginRuntimeModelSwitch\(state, model\)/);
  assert.match(js, /provider\.default_model/);
  assert.match(js, /点击切换到默认模型/);
  assert.match(js, /beginRuntimeModelSwitch\(state, target\)/);
});

test("usage panel renders cached financial facts before triggering account refresh", () => {
  assert.match(html, /id="usage-financials"/);
  assert.match(html, /id="usage-manage"/);
  assert.match(js, /\/api\/usage\/summary\?period=current_month/);
  assert.match(js, /loadUsage\(\)\.then\(refreshUsageAccounts\)/);
  assert.match(js, /本期已用/);
  assert.match(js, /月度额度/);
  assert.match(js, /可用余额/);
  assert.match(js, /无法取得的值不会显示为 0/);
  assert.match(js, /usage_run\.updated/);
});

test("usage settings inspect plugin permissions before trusted installation", () => {
  assert.match(js, /用量账户与自动化插件/);
  assert.match(js, /\/api\/usage\/plugins\/install/);
  assert.match(js, /\/api\/usage\/plugins\/install\?trusted=true/);
  assert.match(js, /SHA-256/);
  assert.match(js, /不承诺抵御恶意 Python 代码/);
  assert.match(js, /secret_schema\.properties/);
});
