"""Runtime-local capability inventory and bounded, non-mutating health probes."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Literal

from pydantic import BaseModel

from mewcode.tools.base import Tool, ToolResult, ToolRiskProfile


def inventory(agent):
    registry = agent.registry
    rows = []
    for tool in registry.list_tools():
        client = getattr(tool, "_client", None)
        alive = getattr(client, "is_alive", None)
        rows.append({"name": tool.name, "registered": True, "enabled": registry.is_enabled(tool.name),
            "schema_loaded": not getattr(tool, "should_defer", False) or registry.is_discovered(tool.name),
            "category": getattr(tool, "category", "unknown"), "permission": "checked_per_call",
            "connection": ("connected" if alive else "disconnected") if isinstance(alive, bool) else "not_probed"})
    browser = [row for row in rows if row["name"] in {"browser_navigate", "browser_snapshot"}]
    return {"tools": rows, "browser_tools_registered": len(browser) == 2 and all(r["enabled"] for r in browser),
            "browser_connected": len(browser) == 2 and all(r["enabled"] and r["connection"] == "connected" for r in browser),
            "model_availability": "not_probed", "scope": "current_runtime_only",
            "verification_routes": [{**route, "request_tool_enabled": registry.is_enabled(route.get("request_tool", "")),
                **({'batch_handoff': {**route['batch_handoff'],
                    'request_tool_enabled': registry.is_enabled(route['batch_handoff']['request_tool'])}}
                   if route.get('batch_handoff') else {})}
                                    for route in getattr(agent, "verification_routes", [])],
            "review_protocol": {key: value for key, value in (getattr(agent, 'review_protocol', None) or {}).items()
                                if key in {'input_kind', 'queue_initialized', 'submission_tool'}},
            "note": "Registered/enabled is not permission or a successful live probe. Missing tools cannot be enabled by discovery."}


class ProbeParams(BaseModel):
    target: Literal["inventory", "browser", "search", "judgment", "decision", "context", "model", "models"] = "inventory"
    live: bool = False


class ProbeCapabilities(Tool):
    name = "ProbeCapabilities"
    description = ("Inspect THIS runtime's tools, search/judgment configuration and scheduled verification routes. "
                   "Use target=models for the complete configured model list without per-tool inventory rows. "
                   "Optional live health check uses only ping/model listing, never starts agents, navigates, generates or writes. "
                   "A tool absent from this node may exist in a downstream verification node; inspect routes before reporting a missing Agent.")
    category = "read"
    params_model = ProbeParams
    consumes_runtime_slot = False

    def __init__(self, agent, providers=None):
        self.agent, self.providers = agent, providers
        self._probes = {}

    def permission_risk(self, arguments):
        return ToolRiskProfile(network=bool(arguments.get("live")), target_type="capability_probe")

    async def execute(self, params):
        if params.target == "models":
            from mewcode.experts.capability_broker import model_inventory
            models = model_inventory(self.providers)
            active = getattr(self.agent, 'runtime_model_ref', None)
            active = ({key: active[key] for key in ('provider_key', 'model_id', 'effort')
                       if isinstance(active.get(key), str)} if isinstance(active, dict) else None)
            listed = (any(row['provider_key'] == active.get('provider_key')
                          and row['model_id'] == active.get('model_id') for row in models) if active else True)
            return ToolResult(json.dumps({
                "models": models,
                "models_complete": self.providers is not None and listed,
                "active_model": ({**active, 'source': 'host_runtime_binding',
                    'present_in_configured_catalog': listed, 'generation_tested': False} if active else None),
                "model_availability": "configured_metadata_only",
                "generation_tested": False,
                "scope": "current_runtime_only",
                "note": "Host registry metadata only; not a live service test, verified vision result, or permission grant. "
                        "An absent registry or omitted active binding is incomplete catalog evidence, "
                        "not proof that the bound model is unavailable. Active binding is host configuration, "
                        "not a server identity or generation attestation.",
            }, ensure_ascii=False, indent=2))
        result = inventory(self.agent)
        if params.target == 'inventory':
            from mewcode.experts.capability_broker import model_inventory
            result['models'] = model_inventory(self.providers)
            result['model_availability'] = 'configured_metadata_only'
        decision = getattr(self.agent, "decision_runtime", None)
        if decision is not None:
            result["decision"] = {**decision.readiness(), "specs": list(decision.specs()),
                                  "tool_enabled": self.agent.registry.is_enabled("DecideNextStep"),
                                  "permissions": "checked_per_call", "max_rounds_per_issue": 2}
        judgments = getattr(self.agent, "judgment_runtime", None)
        if judgments is not None:
            result["typed_judgments"] = {**judgments.readiness(), "service": "judgments",
                "types": ["choice", "noul", "score"], "specs": list(judgments.judgment_specs()),
                "scope": "current_runtime_only", "permissions": "enforced_by_consuming_tool"}
        shadow = getattr(self.agent, "tool_history_shadow", None)
        if shadow is not None:
            result["tool_history_shadow"] = shadow.readiness()
        if params.target == "context" and params.live:
            result["live_probe"] = {"status": "not_run", "reason": "Shadow readiness only; no generation or context mutation."}
        if params.target == "search":
            search = self.agent.registry.get("WebSearch") or self.agent.registry.get("web_search")
            result["search"] = (search.readiness(self.agent.work_dir) if search and self.agent.registry.is_enabled(search.name)
                                and callable(getattr(search, "readiness", None))
                                else {"status": "not_available_in_this_runtime"})
            if params.live:
                result["live_probe"] = {"status": "not_run", "reason": "Search health requires a real query; invoke WebSearch with the authorized task query."}
        elif params.target == "judgment":
            from mewcode.tools.evaluate_evidence import judgment_choice
            result["judgment"] = judgment_choice(self.providers, self.agent.work_dir)[1]
        if params.live and params.target in {"model", "judgment", "decision", "browser"}:
            # Bind cache to the active model/transport, not a stale selection.
            transport = getattr(self.agent.registry.get("browser_snapshot"), "_client", None)
            identity = (params.target, id(getattr(self.agent, "client", None)), id(transport),
                        getattr(transport, "is_alive", False), self.agent.registry.is_enabled("browser_snapshot"),
                        getattr(self.providers, "desktop_revision", lambda: 0)(), str(result.get(params.target, {})))
            old = self._probes.get(identity)
            if old and old[0] > time.monotonic():
                result["live_probe"] = {**old[1], "cached": True}
            else:
                try:
                    async with asyncio.timeout(8):
                        if params.target == "browser":
                            tool = self.agent.registry.get("browser_snapshot")
                            client = getattr(tool, "_client", None)
                            if not tool or not self.agent.registry.is_enabled(tool.name) or not getattr(client, "is_alive", False):
                                raise ValueError("browser_not_connected")
                            await client._session.send_ping()
                        else:
                            client = self.agent.client
                            if params.target == "judgment":
                                ref, _ = judgment_choice(self.providers, self.agent.work_dir)
                                if ref is None:
                                    raise ValueError("judgment_not_configured")
                                client = self.providers.resolve(ref).client
                            elif params.target == "decision":
                                if decision is None or not self.agent.registry.is_enabled("DecideNextStep"):
                                    raise ValueError("decision_not_available_in_this_scope")
                                client, _ = await decision.client()
                                if client is None:
                                    raise ValueError("decision_not_configured")
                            listing = getattr(client, "list_models", None)
                            if not callable(listing):
                                raise ValueError("non_generation_probe_not_supported")
                            await listing()
                    probe = {"status": "healthy", "checked_at": time.time(), "generation_tested": False}
                except Exception as exc:  # noqa: BLE001 -- connector-specific errors are bounded and redacted here.
                    probe = {"status": "unavailable_or_not_probeable", "error_type": type(exc).__name__, "checked_at": time.time()}
                self._probes[identity] = (time.monotonic() + 30, probe)
                result["live_probe"] = probe
        return ToolResult(json.dumps(result, ensure_ascii=False))
