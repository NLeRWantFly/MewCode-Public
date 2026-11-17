from __future__ import annotations

import json

from mewcode.decisions.spec import DecisionInput
from mewcode.tools.base import Tool, ToolResult, ToolRiskProfile


class DecideNextStep(Tool):
    name = "DecideNextStep"
    category = "read"
    consumes_runtime_slot = False
    params_model = DecisionInput
    description = (
        "Ask configured Jev to select among 1-12 structured candidates for a decision or recoverable blocker. "
        "Use BEFORE asking the user to choose technical approaches. Same issue ID across replanning: if all "
        "rejected/uncertain, rethink with evidence and materially different candidates, then evaluate once more. "
        "New states may be evaluated; identical inputs reuse receipts. Host budgets bound calls. "
        "Never supplies user preferences, credentials, permission or independent Verification. "
        "When unavailable or uncertain, continue with the main model. Ask only for concrete user-only facts, business decisions or authorization; never ask the user to choose internal technical recovery. Load jev-decisions for the slot contract."
    )

    def __init__(self, runtime):
        self.runtime = runtime

    def permission_risk(self, arguments):
        return ToolRiskProfile(network=True, cost=True, target_type="auxiliary_decision")

    async def execute(self, params):
        return ToolResult(json.dumps(await self.runtime.decide(params), ensure_ascii=False))
