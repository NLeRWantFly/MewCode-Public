from __future__ import annotations

from pathlib import Path

import pytest

from mewcode.tools.browser import Browser, Params as BrowserParams, WebFetch, WebFetchParams
from mewcode.tools.web_http import format_search_output, responses_endpoint
from mewcode.tools.web_search import Params as SearchParams, WebSearch
from mewcode.tools import create_default_registry


def test_default_registry_includes_codex_web_tools() -> None:
    registry = create_default_registry()
    names = {tool.name for tool in registry.list_tools()}
    expected = {"WebSearch", "web_search", "Browser", "browser", "WebFetch", "web_fetch"}
    assert expected <= names
    schema_names = {schema["name"] for schema in registry.get_all_schemas()}
    assert expected <= schema_names
    openai_names = {schema["name"] for schema in registry.get_all_schemas("openai")}
    assert expected <= openai_names


def test_responses_endpoint_strips_v1() -> None:
    assert responses_endpoint("https://api.deepseek.com/v1") == "https://api.deepseek.com/responses"
    assert responses_endpoint("https://api.deepseek.com") == "https://api.deepseek.com/responses"


def test_format_search_output_extracts_citations() -> None:
    text = format_search_output({
        "output_text": "Python 3.14 is current.",
        "output": [
            {
                "type": "web_search_call",
                "action": {"query": "Python 3.14 release"},
            },
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "See the official notes.",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "title": "What's New",
                                "url": "https://docs.python.org/3/whatsnew/3.14.html",
                            }
                        ],
                    }
                ],
            },
        ],
    })
    assert "Python 3.14 is current." in text
    assert "Sources:" in text
    assert "https://docs.python.org/3/whatsnew/3.14.html" in text


@pytest.mark.asyncio
async def test_web_search_posts_deepseek_web_search_tool() -> None:
    captured: dict[str, object] = {}

    async def post_json(url, api_key, body):
        captured["url"] = url
        captured["key"] = api_key
        captured["body"] = body
        return {
            "output_text": "hit",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "hit", "annotations": []}],
                }
            ],
        }

    tool = WebSearch(
        auth_resolver=lambda _work: ("sk-test", "https://api.deepseek.com/v1", "deepseek-v4-flash"),
        tavily_auth_resolver=lambda _work: "",
        post_json=post_json,
    )
    result = await tool.execute(SearchParams(query="deepseek responses api"))
    assert not result.is_error
    assert "hit" in result.output
    assert captured["url"] == "https://api.deepseek.com/responses"
    body = captured["body"]
    assert body["tools"] == [{"type": "web_search"}]
    assert body["tool_choice"] == {"type": "web_search"}
    assert body["input"] == "deepseek responses api"


@pytest.mark.asyncio
async def test_web_search_missing_key_falls_back() -> None:
    async def fetch(url):
        return '<rss><channel><item><title>Source</title><link>https://example.com/</link><description>Evidence</description></item></channel></rss>'
    tool = WebSearch(
        auth_resolver=lambda _work: ("", "https://api.deepseek.com", "deepseek-v4-flash"), fetch_text=fetch,
        tavily_auth_resolver=lambda _work: "",
    )
    result = await tool.execute(SearchParams(query="Source"))
    assert not result.is_error
    assert "https://example.com/" in result.output


@pytest.mark.asyncio
async def test_browser_open_snapshot_links_and_download(tmp_path: Path, monkeypatch) -> None:
    html = (
        b"<html><head><title>Paper</title></head>"
        b"<body><p>Abstract here</p>"
        b'<a href="/next">Next</a></body></html>'
    )

    async def fetch_bytes(url: str):
        return html, "text/html; charset=utf-8", url

    monkeypatch.chdir(tmp_path)
    tool = Browser(fetch_bytes=fetch_bytes)
    opened = await tool.execute(BrowserParams(action="open", url="https://example.com/paper"))
    assert not opened.is_error
    assert "Opened https://example.com/paper" in opened.output

    snap = await tool.execute(BrowserParams(action="snapshot"))
    assert "Abstract here" in snap.output
    assert "Title: Paper" in snap.output

    links = await tool.execute(BrowserParams(action="links"))
    assert "https://example.com/next" in links.output

    saved = await tool.execute(
        BrowserParams(action="download", url="https://example.com/paper", path="paper.html")
    )
    assert not saved.is_error
    assert (tmp_path / "paper.html").read_bytes() == html


@pytest.mark.asyncio
async def test_browser_rejects_non_http() -> None:
    tool = Browser()
    result = await tool.execute(BrowserParams(action="open", url="file:///etc/passwd"))
    assert result.is_error


@pytest.mark.asyncio
async def test_webfetch_returns_extracted_text() -> None:
    async def fetch_bytes(url: str):
        return b"<html><body><h1>Docs</h1></body></html>", "text/html", url

    fetch = WebFetch(browser=Browser(fetch_bytes=fetch_bytes))
    result = await fetch.execute(WebFetchParams(url="https://example.com/docs"))
    assert not result.is_error
    assert "Docs" in result.output


def test_auto_reviews_network_search_without_explicit_scope(tmp_path: Path) -> None:
    from mewcode.permissions import (
        DangerousCommandDetector,
        PathSandbox,
        PermissionChecker,
        PermissionMode,
        RuleEngine,
    )

    checker = PermissionChecker(
        DangerousCommandDetector(),
        PathSandbox(str(tmp_path)),
        RuleEngine(),
        PermissionMode.AUTO,
    )
    tool = WebSearch(
        auth_resolver=lambda _work: ("sk", "https://api.deepseek.com", "deepseek-v4-flash")
    )
    decision = checker.check(tool, {"query": "python 3"})
    assert decision.effect == "review"


@pytest.mark.asyncio
async def test_browser_and_alias_share_the_same_page() -> None:
    html = b"<html><body><p>Shared page</p></body></html>"

    async def fetch_bytes(url: str):
        return html, "text/html", url

    from mewcode.tools.browser import BrowserAlias

    browser = Browser(fetch_bytes=fetch_bytes)
    alias = BrowserAlias(browser)
    opened = await alias.execute(BrowserParams(action="open", url="https://example.com/a"))
    assert not opened.is_error
    snap = await browser.execute(BrowserParams(action="snapshot"))
    assert "Shared page" in snap.output


def test_manager_role_keeps_codex_web_aliases() -> None:
    from mewcode.expert_group import ExpertExecutionPolicy
    from mewcode.expert_web import _role_registry

    class Host:
        def __init__(self, registry):
            self.registry = registry

    class Session:
        def __init__(self, registry):
            self.agent = Host(registry)
            self.context = None

    parent = create_default_registry()
    registry = _role_registry(
        Session(parent),
        "manager",
        ExpertExecutionPolicy(strategy="fast", allow_delegation=False, max_direct_agents=0,
                              node_tools=("ReadFile", "Glob", "Grep", "WebSearch", "Browser")),
    )
    names = {tool.name for tool in registry.list_tools()}
    assert {"WebSearch", "web_search", "Browser", "browser"} <= names
    assert "WriteFile" not in names
    assert "Bash" not in names


def test_http_browser_is_not_an_interactive_cdp_tool() -> None:
    from mewcode.pipeline.tool_actions import build_tool_actions, is_interactive_tool

    registry = create_default_registry()
    browser = registry.get("Browser")
    fetch = registry.get("WebFetch")
    assert browser is not None and fetch is not None
    assert not is_interactive_tool(browser)
    assert not is_interactive_tool(fetch)
    actions = build_tool_actions(registry)
    titles = {action.title for action in actions.values()}
    assert {"WebSearch", "Browser", "WebFetch"} <= titles


def test_deepseek_auth_reads_workspace_config(tmp_path: Path, monkeypatch) -> None:
    from mewcode.tools.web_http import resolve_deepseek_search_auth

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_SEARCH_MODEL", raising=False)
    cfg = tmp_path / ".mewcode" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text(
        "providers:\n"
        "  - name: DeepSeek\n"
        "    base_url: https://api.deepseek.com/v1\n"
        "    model: deepseek-v4-pro\n"
        "    api_key: sk-from-yaml\n",
        encoding="utf-8",
    )
    key, base, model = resolve_deepseek_search_auth(str(tmp_path))
    assert key == "sk-from-yaml"
    assert base == "https://api.deepseek.com/v1"
    assert model == "deepseek-v4-pro"
