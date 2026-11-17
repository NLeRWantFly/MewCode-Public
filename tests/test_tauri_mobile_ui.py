from pathlib import Path
import json
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static"
ANDROID_CONF = ROOT / "src-tauri" / "tauri.android.conf.json"
BUILD_SCRIPT = ROOT / "scripts" / "build-android.ps1"


def test_android_window_uses_self_contained_https_mobile_entry() -> None:
    config = json.loads(ANDROID_CONF.read_text(encoding="utf-8"))
    window = config["app"]["windows"][0]
    assert window["url"] == "static/mobile.html"
    assert window["useHttpsScheme"] is True
    assert config["build"]["frontendDist"] == "mobile-dist"


def test_mobile_html_is_self_contained_and_hides_desktop_chrome() -> None:
    html = (STATIC / "mobile.html").read_text(encoding="utf-8")
    js = (STATIC / "mobile.js").read_text(encoding="utf-8")
    assert '<section id="mobile-shell"' in html
    assert "<style>" in html
    assert "--bg: #0d0d0d" in html
    assert "body.mobile-runtime" in html or 'class="mobile-runtime"' in html
    assert "id=\"drawer\"" not in html
    assert 'id="main"' not in html
    assert "新建对话" not in html
    assert not re.search(r"""(?:href|src)=["']/static/""", html)
    assert "waitForInvoke" in js
    assert "__TAURI_INTERNALS__" in js
    assert "mobile_request_pairing" in js
    assert 'src="mobile.js' in html
    assert "viewport-fit=cover" in html
    assert 'href="vendor/katex.min.css"' in html
    assert "markdown-render.js" in html
    assert (STATIC / "markdown-render.js").is_file()
    assert (STATIC / "vendor" / "katex.min.js").is_file()
    assert (STATIC / "vendor" / "fonts" / "KaTeX_Main-Regular.woff2").is_file()


def test_mobile_history_detail_uses_fullscreen_sheet_and_collapsible_groups() -> None:
    html = (STATIC / "mobile.html").read_text(encoding="utf-8")
    js = (STATIC / "mobile.js").read_text(encoding="utf-8")
    css = (STATIC / "mobile.css").read_text(encoding="utf-8")
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    chat_css = (STATIC / "chat-messages.css").read_text(encoding="utf-8")
    assert 'id="mobile-detail-sheet"' in html
    assert 'id="mobile-detail-body"' in html
    assert 'id="mobile-detail-back"' in html
    assert 'id="mobile-composer"' in html
    assert 'id="mobile-interrupt"' in html
    assert 'id="mobile-composer-interrupt"' in html
    assert 'id="mobile-resume"' in html
    assert 'id="mobile-orch-board"' in html
    assert 'id="mobile-prompt-sheet"' in html
    assert "允许" in html and "始终允许" in html and "拒绝" in html
    assert 'id="mobile-new-chat"' in html
    assert 'id="mobile-detail"' not in html
    assert "closeDetail" in js
    assert "renderMobileMarkdown" in js
    assert "enhanceMath" in js
    assert ".mobile-detail-sheet" in css
    assert "mobile-source-group" in css
    assert "@media (min-width: 840px)" in css
    assert "grid-template-columns: minmax(280px, 36%) 1fr" in css
    assert "MewMarkdown" in (STATIC / "markdown-render.js").read_text(encoding="utf-8")
    assert "vendor/katex.min.js" in index
    assert "markdown-render.js" in index
    assert "chat-messages.css" in index
    assert "mobile.css" in index
    assert "mobile.js" in index
    assert 'href="chat-messages.css"' in html or 'href="mobile.css"' in html
    assert "class=\"msg ${role}\"" in js or 'class="msg ' in js
    assert "msg-bubble" in js
    assert ".msg.assistant" in chat_css
    assert "details.tool-group" in chat_css
    assert "tg-status" in app_js
    assert "工具调用" in app_js
    assert "enhanceMath" in app_js
    assert "renderMarkdown(text)" in app_js or "MewMarkdown.renderMarkdown" in app_js
    assert "remote_conversation_focus" in app_js


def test_mobile_assets_keep_relative_sources_in_sync() -> None:
    css = (STATIC / "mobile.css").read_text(encoding="utf-8")
    js = (STATIC / "mobile.js").read_text(encoding="utf-8")
    html = (STATIC / "mobile.html").read_text(encoding="utf-8")
    assert "--on-accent: #0d0d0d" in css
    assert "color: var(--on-accent)" in css
    assert "function initMobileRuntime" in js
    assert "runtime: \"tauri-mobile\"" in js
    assert "--on-accent: #0d0d0d" in html
    assert "startMewcodeMobileRuntime" in js
    assert "startMewcodeMobileRuntime" in (STATIC / "app.js").read_text(encoding="utf-8")


def test_android_build_stages_mobile_html_into_asset_url_path() -> None:
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "mobile.html" in script
    assert 'Join-Path $assetRoot "static"' in script
    assert "Remove-Item -LiteralPath $staticDest" in script
    assert r"assets\_up_\frontend" not in script


def test_index_mobile_runtime_hides_desktop_chrome_without_media_query() -> None:
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    hide_rule = "body.mobile-runtime > #drawer, body.mobile-runtime > #drawer-backdrop, body.mobile-runtime > #main"
    assert hide_rule in css
    prefix = css.split(hide_rule, 1)[0]
    assert prefix.rfind("@media") < prefix.rfind(".mobile-shell")
    # index.html is shared with the mobile bootstrap. Mobile's self-contained
    # reset must load before the desktop sheets, otherwise it forces a dark
    # palette and a visible full-height mobile shell onto the desktop page.
    assert index.index("mobile.css") < index.index("_tokens.css")
    assert index.index("mobile.css") < index.index("style.css")


def test_tauri_bridge_uses_internals_invoke() -> None:
    bridge = (STATIC / "tauri-bridge.js").read_text(encoding="utf-8")
    assert "__TAURI_INTERNALS__" in bridge
    assert "resolveInvoke" in bridge


def test_android_capability_allows_core_ipc() -> None:
    capability = json.loads((ROOT / "src-tauri" / "capabilities" / "android.json").read_text(encoding="utf-8"))
    permission = (ROOT / "src-tauri" / "permissions" / "mobile-bridge.toml").read_text(encoding="utf-8")
    build_rs = (ROOT / "src-tauri" / "build.rs").read_text(encoding="utf-8")
    assert capability["identifier"] == "android-main"
    assert "main" in capability["windows"]
    assert "core:default" in capability["permissions"]
    assert "allow-mobile-bridge" in capability["permissions"]
    assert "android" in capability["platforms"]
    assert "mobile_probe_host" in permission
    assert "mobile_discover_nodes" in permission
    assert "mobile_remote_request" in permission
    assert '"mobile_probe_host"' in build_rs


def test_mobile_pairing_discovers_nearby_computers() -> None:
    html = (STATIC / "mobile.html").read_text(encoding="utf-8")
    js = (STATIC / "mobile.js").read_text(encoding="utf-8")
    css = (STATIC / "mobile.css").read_text(encoding="utf-8")
    bridge = (STATIC / "tauri-bridge.js").read_text(encoding="utf-8")
    rust = (ROOT / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'id="mobile-pairing-input"' not in html
    assert 'id="mobile-pairing-input"' not in index
    assert "mewcode://pair" not in html
    assert "粘贴 mewcode://pair" not in html
    assert 'id="mobile-node-list"' in html
    assert 'id="mobile-manual-host"' in html
    assert 'id="mobile-manual-host"' in index
    assert 'id="mobile-host-input"' in html
    assert "MagicDNS" in html
    assert "刷新附近电脑" in html
    assert "mobile_request_pairing" in js
    assert "mobile_probe_host" in js
    assert "mobileDiscoverNodes" in js
    assert "mobileProbeHost" in js
    assert "mobileRequestPairing" in js
    assert "mobilePollPairing" in js
    assert "connectManualHost" in js
    assert "等待电脑批准" in js
    assert "mobileDiscoverNodes" in bridge
    assert "mobileProbeHost" in bridge
    assert "connectManualHost" in app_js
    assert 'id="mobile-detail-sheet"' in html
    assert 'id="mobile-detail-sheet"' in index
    assert 'id="mobile-composer"' in index
    assert "mobile-detail-sheet" in js
    assert "speech_only=true" in js
    assert "speech_only=true" in app_js
    assert "HISTORY_SOURCE_ORDER" in js
    assert "HISTORY_SOURCE_ORDER" in app_js
    assert '["mewcode"]' in js
    assert '["mewcode", "codex", "claude-code"]' not in js
    assert 'data-group-key="source:mewcode"' in js
    assert "配对后会显示电脑上的 MewCode 对话" in html
    assert "配对后会显示 MewCode、Codex 和 Claude Code 对话" not in html
    assert "Codex" not in html
    assert 'details class="mobile-source-group"' in js
    assert "mobile-folder-group" in js
    assert "vendor/katex.min.js" in html
    assert "markdown-render.js" in html
    assert "MewMarkdown" in js
    assert "kind !== \"message\"" in js or "kind === \"message\"" in js
    assert "/api/remote/conversations/message" in js
    assert "/api/remote/live" in js
    assert "renderToolsHtml" in js
    assert "if (!text && !tools.length) return" in js
    assert "step.status === \"running\"" in js
    assert "class=\"tool-group" in js or "tool-group${" in js
    assert "is-member" in js
    assert "mobile-send-projects-1" in html
    assert "syncActionState" in js
    assert "setSending(!!operationId || !!liveState?.running)" not in js
    assert "composerInterrupt" in js
    assert "/api/remote/conversations/interrupt" in js
    assert "/api/remote/conversations/resume" in js
    assert "/api/remote/prompts" in js
    assert "conversations/activate" in rust
    assert "/api/remote/live" in rust
    assert "conversations/interrupt" in rust
    assert 'src="mobile.js' in html or "mobile.js" in html
    assert "mobile_submit_pairing_uri" not in rust
    assert "parse_mobile_pairing_uri" not in rust
    assert "mewcode-pending-pairing-uri.txt" not in rust
    assert "fn mobile_discover_nodes" in rust
    assert "fn mobile_probe_host" in rust
    assert "fn parse_manual_tailscale_host" in rust
    assert "不是 Tailscale 地址" in rust
    assert "from_millis(1500)" not in rust
    assert "MOBILE_PROBE_TIMEOUT" in rust
    assert "http1_only" in rust
    assert "if !is_ip" in rust
    assert "请改用电脑的 MagicDNS" in rust
    assert "async fn mobile_request_pairing" in rust
    assert "font: 16px/1.4" in html
    assert "font: 16px/1.4" in css
    assert "position: relative" in css


def test_mobile_collab_dom_contract() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    result = subprocess.run(
        [node, str(ROOT / "tests" / "mobile_collab_dom.cjs")],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_android_pairing_deep_link_is_removed() -> None:
    rust = (ROOT / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
    activity = (ROOT / "src-tauri" / "gen" / "android" / "app" / "src" / "main" / "java" / "com" / "mewcode" / "desktop" / "MainActivity.kt").read_text(encoding="utf-8")
    assert "parse_mobile_pairing_uri" not in rust
    assert "mewcode-pending-pairing-uri.txt" not in rust
    assert "capturePairingIntent" not in activity
    assert "mewcode://pair" not in activity
    manifest = ROOT / "src-tauri" / "gen" / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
    if manifest.is_file():
        text = manifest.read_text(encoding="utf-8")
        assert 'android:host="pair"' not in text
        assert 'android:scheme="mewcode"' not in text
