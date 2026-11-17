"""Optional Jev batch judgments. Never a permission or independent acceptance gate."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml

from mewcode.experts.recovery_state import digest
from mewcode.model_registry import ModelRef
from mewcode.tools.base import Tool, ToolResult, ToolRiskProfile
from mewcode.tools.evaluate_replan_hypothesis import (
    EvaluateHypothesisParams,
    EvaluateReplanHypothesis,
)


def judgment_choice(providers, work_dir=None, *, config_key="judgment_model"):
    """Read current provider selection; do not create clients or probe the network."""
    if providers is None:
        return None, {"status": "provider_registry_unavailable"}
    explicit = None
    for root in ([Path(work_dir)] if work_dir else []) + [Path.home()]:
        path = root / ".mewcode" / "config.yaml"
        try:
            config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if config_key in config:
                explicit = config[config_key]
                if explicit is False:
                    return None, {"status": "disabled"}
                break
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            continue
    try:
        if explicit is not None:
            ref = ModelRef.from_value(explicit)
            record = providers.get(ref.provider_key)
            if record is None or record.provider.protocol != "typesafe":
                return None, {"status": "configured_route_unavailable", "fallback": False}
        else:
            records = [r for r in providers.records() if r.provider.protocol == "typesafe"]
            if len(records) != 1:
                return None, {"status": "missing_or_ambiguous_judgment_provider", "candidates": len(records)}
            record = records[0]
            ref = ModelRef(record.key, record.provider.model)
        if not record.provider.resolve_api_key():
            return None, {"status": "missing_credential"}
        return ref, {"status": "configured_not_probed", "model": ref.model_id,
                     "provider_key": ref.provider_key, "advisory_only": True, "vision": False}
    except (ValueError, KeyError, AttributeError):
        return None, {"status": "configuration_error"}


class EvaluateEvidence(Tool):
    name = "EvaluateEvidence"
    category = "read"
    consumes_runtime_slot = False
    params_model = EvaluateHypothesisParams
    description = ("Batch up to three independent, evidence-backed hypotheses for configured Jev/TypeSafe. "
        "evidence_refs must be tool-result IDs already observed in THIS conversation. One paid batch per conversation; "
        "duplicates reuse a durable receipt. Text-only advisory probabilities, NOT independent Verification, PASS, "
        "permissions or instructions to execute. Missing credentials never trigger model fallback.")

    def __init__(self, agent, providers):
        self.agent, self.providers = agent, providers

    def permission_risk(self, arguments):
        return ToolRiskProfile(network=True, cost=True, target_type="auxiliary_judgment")

    async def execute(self, params):
        ref, readiness = judgment_choice(self.providers, self.agent.work_dir)
        if ref is None:
            return ToolResult("No auxiliary judgment: " + readiness["status"], is_error=True)
        conversation = getattr(self.agent, "_current_conversation", None)
        session_id = getattr(self.agent, "session_id", "")
        if conversation is None or not session_id:
            return ToolResult("No durable conversation scope available; no judgment sent.", is_error=True)
        requested = {ref for hypothesis in params.hypotheses for ref in hypothesis.evidence_refs}
        evidence = [{"ref": result.tool_use_id, "output": result.content[:4000], "is_error": result.is_error}
            for message in conversation.history for result in message.tool_results if result.tool_use_id in requested]
        if not requested <= {row["ref"] for row in evidence}:
            return ToolResult("Hypotheses reference missing tool-result evidence", is_error=True)
        try:
            selected = self.providers.resolve(ref, usage_recorder=self.agent.client.get_usage_recorder())
        except (ValueError, KeyError):
            return ToolResult("Configured judgment route unavailable; no fallback.", is_error=True)
        from mewcode.runtime_paths import user_data_root
        root = user_data_root() / "evidence-judgments"
        root.mkdir(parents=True, exist_ok=True)
        def charge(tokens):
            self.agent.auxiliary_judgment_tokens = getattr(self.agent, "auxiliary_judgment_tokens", 0) + tokens
        delegate = EvaluateReplanHypothesis(store=SimpleNamespace(root=root),
            run_id="chat:" + digest([str(Path(self.agent.work_dir).resolve()), session_id]),
            node=SimpleNamespace(node_id="chat"), client=selected.client,
            policy=lambda: SimpleNamespace(token_budget=24000, timeout_seconds=20), charge=charge,
            context_provider=lambda _: {"evidence": evidence})
        return await delegate.execute(params)
