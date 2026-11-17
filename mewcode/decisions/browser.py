"""Bounded navigation reference plugin: observed links, no arbitrary clicks/JS."""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel, Field

from mewcode.decisions.runtime import digest
from mewcode.decisions.spec import Candidate, DecisionInput
from mewcode.tools.base import Tool, ToolCallComplete, ToolResult, ToolRiskProfile


@asynccontextmanager
async def browser_transaction(client):
    """Serialize whole observe/decide/act loops with ordinary MCP browser calls."""
    task = asyncio.current_task()
    if getattr(client, "_decision_browser_owner", None) is task:
        yield
        return
    lock = getattr(client, "_decision_browser_lock", None)
    if lock is None:
        lock = client._decision_browser_lock = asyncio.Lock()
    async with lock:
        client._decision_browser_owner = task
        try:
            yield
        finally:
            client._decision_browser_owner = None


def navigation_candidates(snapshot: str, allowed_hosts: set[str]):
    match = re.search(r"(?:Page URL:|URL:)\s*(https?://\S+)", snapshot)
    if not match:
        return "", []
    current = match[1].strip('`')
    if urlsplit(current).hostname not in allowed_hosts:
        return current, []
    rows, seen = [], {current}
    # Playwright MCP's observed accessibility snapshot link + /url representation.
    for match in re.finditer(r'(?m)^\s*- link "([^"\n]+)"[^\n]*\n\s*- /url: (\S+)', snapshot):
        label, href = match.groups()
        url = urljoin(current, href.strip('"'))
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or parsed.hostname not in allowed_hosts or parsed.username or parsed.password:
            continue
        if re.search(r"(?i)(?:delete|remove|logout|signout|purchase|checkout|unsubscribe|confirm|submit)", url):
            continue
        if url in seen:
            continue
        seen.add(url)
        rows.append(Candidate(id=f"link_{len(rows)}", description=label[:500], facts={"observed_url": url}))
        if len(rows) >= 10:
            break
    return current, rows


class FastBrowseParams(BaseModel):
    goal: str = Field(min_length=1, max_length=1500)
    issue: str = Field(min_length=1, max_length=100)
    allowed_hosts: list[str] = Field(min_length=1, max_length=8, description="Exact hostnames within the user's browsing task, no wildcards")
    max_steps: int = Field(default=4, ge=1, le=8)


class FastBrowse(Tool):
    name = "FastBrowse"
    category = "harness"
    interaction_kind = "browser"
    consumes_runtime_slot = False
    params_model = FastBrowseParams
    description = (
        "Jev-driven bounded navigation on the current Playwright page, without a main-model round per link. "
        "First open a task page using browser_navigate; supply exact allowed_hosts. Only follows observed "
        "HTTP(S) links via browser_navigate; no forms, typing, purchases, logins, file operations or arbitrary JS. "
        "Every nested tool still uses host permission checks. Returns evidence for main-model verification, "
        "never Verification PASS. Unsupported snapshots or unknown action outcomes return to the main model."
    )

    def __init__(self, agent, runtime):
        self.agent, self.runtime = agent, runtime

    def permission_risk(self, arguments):
        return ToolRiskProfile(side_effect="external", network=True, cost=True, target_type="browser", reversible=True)

    async def call(self, name, arguments):
        return await self.agent._execute_tool_noninteractive(ToolCallComplete(
            tool_id="fastbrowse_" + uuid.uuid4().hex, tool_name=name, arguments=arguments))

    async def execute(self, params):
        registry = self.agent.registry
        if not all(registry.is_enabled(name) for name in ("browser_snapshot", "browser_navigate")):
            return ToolResult("FastBrowse requires enabled browser_snapshot and browser_navigate in THIS runtime.", is_error=True)
        snapshot_tool = registry.get("browser_snapshot")
        client = getattr(snapshot_tool, "_client", None)
        if client is None or client is not getattr(registry.get("browser_navigate"), "_client", None):
            return ToolResult("Browser tools must share one live browser connection.", is_error=True)
        steps, seen = [], set()
        try:
            async with asyncio.timeout(60), browser_transaction(client):
                for _ in range(params.max_steps):
                    observed = await self.call("browser_snapshot", {})
                    if observed.is_error:
                        return self.result("main_model", steps, "snapshot_failed")
                    snapshot = observed.output
                    current, candidates = navigation_candidates(snapshot, {h.lower() for h in params.allowed_hosts})
                    if not current or urlsplit(current).hostname not in {h.lower() for h in params.allowed_hosts}:
                        return self.result("main_model", steps, "unsupported_snapshot_or_outside_allowed_hosts")
                    fingerprint = digest(snapshot)
                    if fingerprint in seen:
                        return self.result("main_model", steps, "no_progress")
                    seen.add(fingerprint)
                    candidates.append(Candidate(id="observed_done", description="Visible page evidence already answers the goal; return it for independent main-model verification"))
                    decision = await self.runtime.decide(DecisionInput(
                        spec="browser-navigation", issue=params.issue + ":" + fingerprint[:16], goal=params.goal,
                        state={"url": current, "visible_snapshot": snapshot[:7000], "snapshot_truncated": len(snapshot) > 7000},
                        candidates=candidates), observed_evidence=[{"ref": fingerprint, "source": "host.browser_snapshot"}])
                    steps.append({"url": current, "decision": decision})
                    if decision.get("status") != "selected":
                        return self.result("main_model", steps, decision.get("reason", "rethink"))
                    if decision["choice"] == "observed_done":
                        return self.result("observed_done", steps, "requires_main_model_verification", snapshot[:12000])
                    selected = next(c for c in candidates if c.id == decision["choice"])
                    fresh = await self.call("browser_snapshot", {})
                    if fresh.is_error or digest(fresh.output) != fingerprint:
                        return self.result("main_model", steps, "stale_observation")
                    # Execute a concrete, host-observed URL, not model-authored selectors/actions.
                    navigated = await self.call("browser_navigate", {"url": selected.facts["observed_url"]})
                    if navigated.is_error:
                        return self.result("main_model", steps, "navigation_failed_or_unknown_do_not_replay")
                return self.result("main_model", steps, "step_budget_exhausted")
        except TimeoutError:
            return self.result("main_model", steps, "time_budget_exhausted_outcome_may_be_unknown")

    @staticmethod
    def result(status, steps, reason, evidence=""):
        return ToolResult(json.dumps({"status": status, "reason": reason, "steps": steps,
                                      "evidence": evidence, "verification_pass": False}, ensure_ascii=False))
