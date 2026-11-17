from pathlib import Path

import pytest

from mewcode.tools.web_http import resolve_tavily_auth


@pytest.fixture(autouse=True)
def isolated_search_home(fake_home, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    return fake_home


def write_config(root: Path, filename: str, content: str) -> Path:
    path = root / ".mewcode" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_user_search_config_works_across_workdirs_without_chat_config(fake_home, tmp_path):
    write_config(fake_home, "search.yaml", "search:\n  tavily:\n    api_key: test-search-only\n")
    assert resolve_tavily_auth(str(tmp_path / "project-one")) == "test-search-only"
    assert resolve_tavily_auth(str(tmp_path / "project-two")) == "test-search-only"
    assert not (fake_home / ".mewcode/config.yaml").exists()


def test_environment_and_local_project_priority(fake_home, tmp_path, monkeypatch):
    project = tmp_path / "project"
    write_config(fake_home, "search.yaml", "search: {tavily: {api_key: global}}")
    write_config(project, "config.yaml", "search: {tavily: {api_key: project}}")
    write_config(project, "config.local.yaml", "search: {tavily: {api_key: local}}")
    assert resolve_tavily_auth(str(project)) == "local"
    monkeypatch.setenv("TAVILY_API_KEY", "environment")
    assert resolve_tavily_auth(str(project)) == "environment"


def test_explicit_project_disable_prevents_file_fallback(fake_home, tmp_path):
    write_config(fake_home, "search.yaml", "search: {tavily: {api_key: global}}")
    project = tmp_path / "project"
    write_config(project, "config.yaml", "search: {tavily: {enabled: false}}")
    assert not resolve_tavily_auth(str(project))


@pytest.mark.parametrize("broken", ["[not-a-mapping]", "search: {tavily: [not-a-mapping]}", "search: [", ""])
def test_invalid_file_falls_back_without_reusing_chat_credentials(fake_home, tmp_path, broken):
    write_config(fake_home, "search.yaml", "search: {tavily: {api_key: global}}")
    project = tmp_path / "project"
    write_config(project, "config.local.yaml", broken)
    write_config(project, "config.yaml", "providers: [{api_key: private-chat-only}]")
    assert resolve_tavily_auth(str(project)) == "global"


def test_user_key_rotation_is_read_without_restart(fake_home, monkeypatch):
    write_config(fake_home, "search.yaml", "search: {tavily: {api_key: first}}")
    assert resolve_tavily_auth() == "first"
    write_config(fake_home, "search.yaml", 'search: {tavily: {api_key: "${TEST_SEARCH_KEY}"}}')
    monkeypatch.setenv("TEST_SEARCH_KEY", "second")
    assert resolve_tavily_auth() == "second"
