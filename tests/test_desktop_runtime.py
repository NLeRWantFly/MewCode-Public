import re
from pathlib import Path

from frontend.server import _resolve_static_dir
from mewcode.runtime_paths import user_data_root


def test_frozen_static_directory_matches_pyinstaller_data_layout(tmp_path: Path) -> None:
    assert _resolve_static_dir(tmp_path) == tmp_path / "frontend" / "static"


def test_source_static_directory_is_frontend_assets() -> None:
    static_dir = _resolve_static_dir()
    assert static_dir.name == "static"
    assert (static_dir / "index.html").is_file()


def test_expert_stream_rendering_is_batched() -> None:
    app_js = (_resolve_static_dir() / "app.js").read_text(encoding="utf-8")
    assert "textRenderTimers" in app_js
    assert "setTimeout(" in app_js
    assert "flushExpertTextV2" in app_js


def test_chat_websocket_reconnects_with_backoff() -> None:
    static_dir = _resolve_static_dir()
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    assert "scheduleReconnect" in app_js
    assert "Math.min(1000 * (2 ** reconnectAttempt), 15000)" in app_js
    assert 'window.addEventListener("online"' in app_js
    assert 'id="conn-retry"' in index_html


def test_expert_history_is_lazy_and_resume_requires_confirmation() -> None:
    app_js = (_resolve_static_dir() / "app.js").read_text(encoding="utf-8")
    assert "expertTimelines" in app_js
    assert "loadExpertRoleDetails" in app_js
    assert '"?role=" + encodeURIComponent(role)' in app_js
    assert "继续专家团任务？" in app_js
    assert "系统会从原检查点进入宿主恢复检查" in app_js
    assert 'querySelector(".expert-stage-output").open' in app_js
    assert "setInterval(tickExpertClocks, 1000)" in app_js
    assert "document.hidden || !hasRunningNode" in app_js
    assert 'document.addEventListener("visibilitychange", syncExpertClockTimer)' in app_js


def test_desktop_user_data_override(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "desktop-state"
    monkeypatch.setenv("MEWCODE_USER_DATA_DIR", str(target))
    assert user_data_root() == target.resolve()


def test_windows_reuses_desktop_appdata_when_home_has_no_expert_runs(
    monkeypatch, tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".mewcode" / "expert-runs-v1").mkdir(parents=True)
    roaming = tmp_path / "Roaming"
    desktop = roaming / "com.mewcode.desktop"
    runs = desktop / "expert-runs-v1"
    runs.mkdir(parents=True)
    (runs / f"{'a' * 32}.json").write_text("{}", encoding="utf-8")
    monkeypatch.delenv("MEWCODE_USER_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setattr("mewcode.runtime_paths.os.name", "nt")
    monkeypatch.setattr("mewcode.runtime_paths.Path.home", lambda: home)
    assert user_data_root() == desktop.resolve()


def test_openrouter_account_catalog_contract() -> None:
    static_dir = _resolve_static_dir()
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    server = Path("frontend/server.py").read_text(encoding="utf-8")
    assert "请先测试连接以拉取账号模型列表" in app_js
    assert "请选择默认模型" in app_js
    assert "已验证账号模型目录" in app_js
    assert "openrouterModels" in app_js
    app_script_versions = re.findall(
        r'<script\b[^>]*\bsrc="/static/app\.js\?v=([^"<>]+)"[^>]*>\s*</script>',
        index_html,
    )
    assert len(app_script_versions) == 1
    assert app_script_versions[0].strip()
    assert "只拉取账号模型目录" in server
