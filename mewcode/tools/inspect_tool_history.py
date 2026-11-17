from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel

from mewcode.tools.base import Tool, ToolResult, ToolRiskProfile


class Params(BaseModel):
    action: Literal["report", "sample"] = "report"


class InspectToolHistory(Tool):
    name = "InspectToolHistory"
    category = "read"
    consumes_runtime_slot = False
    params_model = Params
    description = ("Read THIS conversation's tool-history pruning shadow report (no model request). "
                   "Optional sample schedules bounded Jev judgments when enough eligible history exists. "
                   "Shadow never changes messages, files, tool pairs or summary behavior. Savings are hypothetical; "
                   "risk needs independent labels, rereads are only a proxy. Cannot enable real pruning.")

    def __init__(self, observer):
        self.observer = observer

    def permission_risk(self, arguments):
        sample = arguments.get("action") == "sample"
        return ToolRiskProfile(network=sample, cost=sample, target_type="tool_history_shadow")

    async def execute(self, params):
        if params.action == "sample":
            self.observer.sample_latest()
        return ToolResult(json.dumps(await self.observer.report(), ensure_ascii=False))
