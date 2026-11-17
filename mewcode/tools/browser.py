from __future__ import annotations

from html.parser import HTMLParser
from typing import Any, Literal
import time
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urljoin, urlparse

import httpx

from pydantic import BaseModel, Field

from mewcode.tools.base import (
    MAX_OUTPUT_CHARS,
    Tool,
    ToolResult,
    ToolRiskProfile,
    active_work_dir,
    resolve_tool_path,
)
from mewcode.tools.web_http import http_client, is_http_url

_scoped_network_host: ContextVar[str] = ContextVar("mewcode_scoped_network_host", default="")


@contextmanager
def scoped_network_host(host: str):
    """Bind a task-approved fetch to one host, with no redirects or env proxy."""
    token = _scoped_network_host.set(host.lower())
    try:
        yield
    finally:
        _scoped_network_host.reset(token)


class Params(BaseModel):
    action: Literal["open", "snapshot", "links", "download"] = Field(
        description=(
            "open: fetch a URL and keep it as the current page; "
            "snapshot: return extracted text of the current (or given) page; "
            "links: list http(s) links; "
            "download: save the response body into the workspace."
        )
    )
    url: str = Field(default="", description="Page or file URL (required for open/download)")
    path: str = Field(
        default="",
        description="Workspace-relative destination for download",
    )
    max_chars: int = Field(default=8000, ge=500, le=MAX_OUTPUT_CHARS)


class _HTMLExtract(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.title = ""
        self._in_title = False
        self._pending_href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = {key: value or "" for key, value in attrs}
        if tag in {"script", "style", "noscript"}:
            self._skip += 1
            return
        if tag == "title":
            self._in_title = True
        if tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section"}:
            self.parts.append("\n")
        if tag == "a":
            self._pending_href = attrs_d.get("href", "")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag == "a":
            self._pending_href = ""

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = " ".join(data.split())
        if not text:
            return
        if self._in_title:
            self.title += text
        if self._pending_href:
            self.links.append((text, self._pending_href))
        self.parts.append(text + " ")


class Browser(Tool):
    """Codex-style page access: fetch, read, and download. Not a logged-in CDP browser."""

    name = "Browser"
    description = (
        "Open a public http(s) URL, snapshot readable text, list links, or download "
        "a file into the workspace. Use after WebSearch when you have a source URL. "
        "This is the MewCode equivalent of Codex web_fetch / browser page access. "
        "It does not use the user's logged-in Chrome; JS-only apps may be incomplete."
    )
    params_model = Params
    category = "read"
    is_concurrency_safe = False

    def __init__(self, *, fetch_bytes=None) -> None:
        self._fetch_bytes = fetch_bytes
        self._current_url = ""
        self._current_body = b""
        self._current_ctype = ""
        self._fetch_cache = {}

    def permission_risk(self, arguments: dict[str, Any]) -> ToolRiskProfile:
        action = str(arguments.get("action") or "open")
        if action == "download":
            return ToolRiskProfile(
                side_effect="workspace",
                network=True,
                reversible=True,
                target_type="browser",
            )
        return ToolRiskProfile(
            side_effect="none",
            network=True,
            reversible=True,
            target_type="browser",
        )

    async def execute(self, params: Params) -> ToolResult:
        if params.action == "open":
            return await self._open(params.url, params.max_chars)
        if params.action == "snapshot":
            url = params.url or self._current_url
            if url and url != self._current_url:
                opened = await self._open(url, params.max_chars)
                if opened.is_error:
                    return opened
            return self._snapshot(params.max_chars)
        if params.action == "links":
            url = params.url or self._current_url
            if url and url != self._current_url:
                opened = await self._open(url, params.max_chars)
                if opened.is_error:
                    return opened
            return self._links()
        if params.action == "download":
            return await self._download(params.url or self._current_url, params.path)
        return ToolResult(output=f"Error: unknown action {params.action}", is_error=True)

    async def _open(self, url: str, max_chars: int) -> ToolResult:
        err = self._require_url(url)
        if err:
            return err
        try:
            body, ctype, final_url = await self._get(url)
        except Exception as exc:
            return ToolResult(output=f"Error: failed to open {url}: {exc}", is_error=True)
        self._current_url = final_url
        self._current_body = body
        self._current_ctype = ctype
        extracted = _extract_text(body, ctype, final_url, max_chars)
        title = extracted.split("\n", 1)[0]
        return ToolResult(
            output=f"Opened {final_url}\n{title}\nUse snapshot/links/download for more."
        )

    def _snapshot(self, max_chars: int) -> ToolResult:
        if not self._current_url:
            return ToolResult(output="Error: no page is open; call open first", is_error=True)
        text = _extract_text(
            self._current_body, self._current_ctype, self._current_url, max_chars
        )
        return ToolResult(output=f"URL: {self._current_url}\n\n{text}")

    def _links(self) -> ToolResult:
        if not self._current_url:
            return ToolResult(output="Error: no page is open; call open first", is_error=True)
        parser = _HTMLExtract()
        try:
            parser.feed(self._current_body.decode("utf-8", errors="replace"))
        except Exception:
            return ToolResult(output="No HTML links on this page.")
        lines = []
        seen: set[str] = set()
        for label, href in parser.links:
            absolute = urljoin(self._current_url, href)
            if not is_http_url(absolute) or absolute in seen:
                continue
            seen.add(absolute)
            lines.append(f"- {label or absolute}: {absolute}")
            if len(lines) >= 50:
                break
        return ToolResult(
            output="\n".join(lines) if lines else "No http(s) links found."
        )

    async def _download(self, url: str, dest: str) -> ToolResult:
        err = self._require_url(url)
        if err:
            return err
        if not dest.strip():
            parsed = urlparse(url)
            dest = parsed.path.rsplit("/", 1)[-1] or "download.bin"
        path = resolve_tool_path(dest)
        work = active_work_dir()
        if work:
            try:
                path.resolve().relative_to(resolve_tool_path(work).resolve())
            except ValueError:
                return ToolResult(
                    output="Error: download path must stay inside the workspace",
                    is_error=True,
                )
        try:
            body, ctype, final_url = await self._get(url)
        except Exception as exc:
            return ToolResult(output=f"Error: download failed: {exc}", is_error=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        self._current_url = final_url
        self._current_body = body
        self._current_ctype = ctype
        return ToolResult(
            output=f"Saved {len(body)} bytes from {final_url} to {path} ({ctype})"
        )

    def _require_url(self, url: str) -> ToolResult | None:
        if not is_http_url(url):
            return ToolResult(
                output="Error: only http(s) URLs are allowed",
                is_error=True,
            )
        return None

    async def _get(self, url: str) -> tuple[bytes, str, str]:
        key = (active_work_dir(), url, _scoped_network_host.get())
        cached = self._fetch_cache.get(key)
        if cached and cached[0] > time.monotonic():
            if isinstance(cached[1], Exception):
                raise RuntimeError("URL temporarily unavailable (cached failure; no repeated request). Use another source or browser MCP.")
            return cached[1]
        try:
            value = await self._fetch_url(url)
        except Exception as exc:
            self._fetch_cache[key] = (time.monotonic() + 60, exc)
            raise
        if len(self._fetch_cache) >= 32:
            self._fetch_cache.pop(next(iter(self._fetch_cache)))
        self._fetch_cache[key] = (time.monotonic() + 300, value)
        return value

    async def _fetch_url(self, url: str) -> tuple[bytes, str, str]:
        scoped_host = _scoped_network_host.get()
        if scoped_host:
            parsed = urlparse(url)
            if parsed.hostname != scoped_host:
                raise ValueError("Fetch target is outside the approved network host")
            from mewcode.tools.network_transport import PolicyTransport
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=False, trust_env=False,
                                         transport=PolicyTransport()) as client:
                response = await client.get(url, headers={"User-Agent": "MewCode-Browser/1.0"})
                if response.is_redirect:
                    raise ValueError("Scoped network authorization does not include redirect targets")
                response.raise_for_status()
                return response.content, response.headers.get("content-type", "application/octet-stream"), str(response.url)
        if self._fetch_bytes is not None:
            return await self._fetch_bytes(url)
        async with http_client(timeout=60.0) as client:
            response = await client.get(
                url,
                headers={"User-Agent": "MewCode-Browser/1.0"},
            )
            response.raise_for_status()
            ctype = response.headers.get("content-type", "application/octet-stream")
            return response.content, ctype, str(response.url)


def _extract_text(body: bytes, ctype: str, url: str, max_chars: int) -> str:
    lowered = ctype.lower()
    if "html" in lowered or url.lower().endswith((".html", ".htm")) or body[:32].lstrip().startswith((b"<", b"<!")):
        parser = _HTMLExtract()
        parser.feed(body.decode("utf-8", errors="replace"))
        title = parser.title.strip() or url
        text = " ".join("".join(parser.parts).split())
        output = f"Title: {title}\n\n{text}"
    else:
        try:
            output = body.decode("utf-8")
        except UnicodeDecodeError:
            output = f"Binary content ({ctype}), {len(body)} bytes. Use download to save it."
    if len(output) > max_chars:
        output = output[:max_chars] + "\n…(truncated)"
    return output


class BrowserAlias(Tool):
    """Same session as Browser, under the Codex snake_case name."""

    name = "browser"
    description = Browser.description
    params_model = Params
    category = "read"
    is_concurrency_safe = False

    def __init__(self, browser: Browser) -> None:
        self._browser = browser

    def permission_risk(self, arguments: dict[str, Any]) -> ToolRiskProfile:
        return self._browser.permission_risk(arguments)

    async def execute(self, params: Params) -> ToolResult:
        return await self._browser.execute(params)


class WebFetchParams(BaseModel):
    url: str = Field(description="http(s) URL to fetch")
    max_chars: int = Field(default=8000, ge=500, le=MAX_OUTPUT_CHARS)


class WebFetch(Tool):
    name = "WebFetch"
    description = (
        "Fetch a public http(s) URL and return extracted readable text. "
        "Codex-style web_fetch. Prefer Browser when you need links or a download."
    )
    params_model = WebFetchParams
    category = "read"
    is_concurrency_safe = True

    def __init__(self, browser: Browser | None = None) -> None:
        self._browser = browser or Browser()

    def permission_risk(self, arguments: dict[str, Any]) -> ToolRiskProfile:
        return ToolRiskProfile(network=True, reversible=True, target_type="browser")

    async def execute(self, params: WebFetchParams) -> ToolResult:
        opened = await self._browser.execute(
            Params(action="open", url=params.url, max_chars=params.max_chars)
        )
        if opened.is_error:
            return opened
        return self._browser._snapshot(params.max_chars)


class WebFetchAlias(WebFetch):
    name = "web_fetch"
    description = WebFetch.description
