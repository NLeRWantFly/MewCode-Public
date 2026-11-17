from __future__ import annotations

from typing import Any, Literal
import asyncio
import hashlib
import time
from urllib.parse import urlencode

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult, ToolRiskProfile, active_work_dir
from mewcode.tools.web_http import (
    format_search_output,
    http_client,
    resolve_deepseek_search_auth,
    responses_endpoint,
    resolve_tavily_auth,
)


class Params(BaseModel):
    query: str = Field(description="Search query")
    num_results: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Preferred number of results (server may return fewer)",
    )
    provider: Literal["auto", "tavily", "deepseek", "public"] = "auto"
    search_depth: Literal["basic", "advanced"] = "basic"
    include_domains: list[str] = Field(default_factory=list, max_length=20)
    exclude_domains: list[str] = Field(default_factory=list, max_length=20)
    time_range: Literal["day", "week", "month", "year"] | None = None


class WebSearch(Tool):
    """Public web search with optional configured provider and bounded fallback."""

    name = "WebSearch"
    description = (
        "Search the public web using available providers and keyless public search. "
        "Use for current facts, docs, papers, and source URLs. "
        "Repeated queries are cached; unavailable providers are cooled down. "
        "If search is unavailable, use available browser MCP tools to search; do not repeat configuration failures."
    )
    params_model = Params
    category = "read"
    is_concurrency_safe = True

    def __init__(
        self,
        *,
        auth_resolver=resolve_deepseek_search_auth,
        post_json=None,
        fetch_text=None,
        delegate=None,
        browser_available=None,
        tavily_auth_resolver=resolve_tavily_auth,
    ) -> None:
        self._auth_resolver = auth_resolver
        self._default_auth = auth_resolver is resolve_deepseek_search_auth
        self._post_json = post_json
        self._fetch_text = fetch_text
        self._delegate = delegate
        self._browser_available = browser_available
        self._tavily_auth_resolver = tavily_auth_resolver
        self._providers = None
        self._cache = {}
        self._cooldown = {}
        self._inflight = {}
        self._lock = asyncio.Lock()

    def readiness(self, work_dir=None):
        if self._delegate is not None:
            return self._delegate.readiness(work_dir)
        try:
            key, _, _ = self._auth_resolver(work_dir)
            tavily = self._tavily_auth_resolver(work_dir)
            return {"tavily": "configured_not_probed" if tavily else "missing_credential",
                    "deepseek": "configured_not_probed" if key else "missing_or_ambiguous_credential",
                    "public": "available_not_probed"}
        except Exception:
            return {"status": "configuration_error"}

    def bind_providers(self, providers):
        self._providers = providers
        if self._default_auth:
            self._auth_resolver = lambda root: resolve_deepseek_search_auth(root, providers)

    def bound_copy(self, providers, browser_available):
        """Per-runtime caches, credentials and fallback diagnostics; never mutate a parent."""
        source = self._delegate or self
        tool = type(source)(auth_resolver=(resolve_deepseek_search_auth if source._default_auth else source._auth_resolver),
            post_json=source._post_json, fetch_text=source._fetch_text,
            tavily_auth_resolver=source._tavily_auth_resolver, browser_available=browser_available)
        tool.name = self.name
        tool.bind_providers(providers)
        return tool

    def permission_risk(self, arguments: dict[str, Any]) -> ToolRiskProfile:
        return ToolRiskProfile(
            side_effect="none",
            network=True,
            cost=True,
            reversible=True,
            target_type="web_search",
        )

    async def execute(self, params: Params) -> ToolResult:
        if self._delegate is not None:
            return await self._delegate.execute(params)
        revision = self._providers.desktop_revision() if self._providers is not None else 0
        try:
            credentials = repr((self._auth_resolver(active_work_dir()), self._tavily_auth_resolver(active_work_dir())))
            auth_revision = hashlib.sha256(credentials.encode()).hexdigest()
        except Exception:
            auth_revision = "configuration_error"
        cache_key = (params.model_dump_json(), active_work_dir(), revision, auth_revision)
        async with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached[0] > time.monotonic():
                return ToolResult(output="[cached search; no repeated network request]\n" + cached[1].output,
                                  is_error=cached[1].is_error)
            waiter = self._inflight.get(cache_key)
            owner = waiter is None
            if owner:
                waiter = asyncio.get_running_loop().create_future()
                self._inflight[cache_key] = waiter
        if not owner:
            return await asyncio.shield(waiter)
        try:
            result = await self._search(params)
        except asyncio.CancelledError:
            waiter.cancel()
            async with self._lock:
                self._inflight.pop(cache_key, None)
            raise
        except Exception:
            result = ToolResult(output="SEARCH_UNAVAILABLE: search request failed", is_error=True)
        async with self._lock:
            self._inflight.pop(cache_key, None)
            if len(self._cache) >= 128:
                self._cache.pop(next(iter(self._cache)))
            self._cache[cache_key] = (time.monotonic() + (30 if result.is_error else 300), result)
            if not waiter.done():
                waiter.set_result(result)
            return result

    async def _search(self, params: Params) -> ToolResult:
        from mewcode.tools.public_search import bing_results, duck_results, format_results
        query = params.query.strip()
        if not query:
            return ToolResult(output="Error: query is empty", is_error=True)
        notes = []
        tavily_key = self._tavily_auth_resolver(active_work_dir())
        tavily_route = ("tavily", hashlib.sha256(tavily_key.encode()).hexdigest())
        if params.provider in {"auto", "tavily"} and tavily_key and self._cooldown.get(tavily_route, 0) <= time.monotonic():
            try:
                body = {"query": query, "max_results": params.num_results, "search_depth": params.search_depth,
                        "include_answer": False, "include_domains": params.include_domains,
                        "exclude_domains": params.exclude_domains}
                if params.time_range:
                    body["time_range"] = params.time_range
                payload = await self._request("https://api.tavily.com/search", tavily_key, body)
                rows = [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("content", "")}
                        for r in payload.get("results", [])]
                return ToolResult(format_results(rows, query, "tavily", params.num_results))
            except Exception:
                self._cooldown[tavily_route] = time.monotonic() + 60
                notes.append("Tavily unavailable (60s cooldown).")
        if params.provider == "tavily":
            return ToolResult("TAVILY_UNAVAILABLE: missing credential, cooldown or request failure; no alternate provider used.", is_error=True)
        # Do not silently drop caller-required search filters on a fallback.
        if params.include_domains or params.exclude_domains or params.time_range or params.search_depth != "basic":
            return ToolResult("SEARCH_CAPABILITY_MISSING: requested filters require configured Tavily; filters were not ignored.", is_error=True)
        try:
            key, base, model = self._auth_resolver(active_work_dir())
        except Exception:
            key, base, model = "", "", ""
            notes.append("Optional search provider configuration invalid; using public search.")
        route = (base, model, hashlib.sha256(key.encode()).hexdigest(), active_work_dir())
        if params.provider != "public" and key and self._cooldown.get(route, 0) <= time.monotonic():
            try:
                result = await self._deepseek(query, params.num_results, key, base, model)
                if not result.is_error and result.output != "No search results returned.":
                    return result
                notes.append("Configured search provider unavailable; using public search.")
            except Exception:
                notes.append("Configured search provider failed; using public search.")
            self._cooldown[route] = time.monotonic() + 300
        if params.provider == "deepseek":
            return ToolResult("DEEPSEEK_SEARCH_UNAVAILABLE: missing/ambiguous credential, cooldown or request failure; no alternate provider used.", is_error=True)
        for name, url, parser in [
            ("bing-rss", "https://www.bing.com/search?" + urlencode({"q": query, "format": "rss"}), bing_results),
            ("duckduckgo-html", "https://html.duckduckgo.com/html/?" + urlencode({"q": query}), duck_results),
        ]:
            if self._cooldown.get(name, 0) > time.monotonic():
                continue
            try:
                if self._fetch_text is not None:
                    text = await self._fetch_text(url)
                else:
                    async with http_client(timeout=15.0) as client:
                        response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                        response.raise_for_status()
                        text = response.text
                return ToolResult(output="\n".join(notes + [format_results(parser(text), query, name, params.num_results)]))
            except Exception:
                self._cooldown[name] = time.monotonic() + 60
                notes.append(f"{name} unavailable or returned no usable results (60s cooldown).")
        browser = bool(self._browser_available and self._browser_available())
        action = ("Use browser_navigate then browser_snapshot through the normal tool permission flow. "
                  "Do not bypass a CAPTCHA or access restriction. " if browser else
                  "Browser MCP is not currently available in this node; report the capability gap or use known source URLs. ")
        return ToolResult(output="SEARCH_UNAVAILABLE: " + " ".join(notes) + "\n" + action +
                          "Search URL: https://www.bing.com/search?" + urlencode({"q": query}) +
                          "\nDo not repeat this query or request missing API keys; preserve verified sources and report unresolved gaps.", is_error=True)

    async def _deepseek(self, query, num_results, key, base, model):
        body = {
            "model": model,
            "instructions": (
                "You are a web search helper. Search the public web for the query. "
                "Return a concise answer plus a Sources list of titles and URLs. "
                f"Prefer about {num_results} distinct sources."
            ),
            "input": query,
            "tools": [{"type": "web_search"}],
            "tool_choice": {"type": "web_search"},
        }
        try:
            payload = await self._request(responses_endpoint(base), key, body)
        except Exception:
            return ToolResult(output="Error: DeepSeek search failed", is_error=True)
        if isinstance(payload, dict) and payload.get("error"):
            return ToolResult(
                output="Error: DeepSeek search rejected the request",
                is_error=True,
            )
        return ToolResult(output=format_search_output(payload))

    async def _request(self, url: str, api_key: str, body: dict[str, Any]) -> Any:
        if self._post_json is not None:
            return await self._post_json(url, api_key, body)
        async with http_client(timeout=90.0) as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            response.raise_for_status()
            return response.json()


class WebSearchAlias(WebSearch):
    name = "web_search"
    description = WebSearch.description
