"""Model-visible, host-scoped access to the current candidate stage graph."""
from __future__ import annotations

import os
import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mewcode.orchestration.graph_inspection import READER_SERVICE, VALIDATOR_SERVICE, encode
from mewcode.tools.base import Tool, ToolResult, active_work_dir


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["read", "validate"] = "read"
    proposal: dict | None = Field(default=None, description="For validate: complete mewcode.graph-repair.v1 proposal; pure trial, never adoption.")
    ticket: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$", description="Repair ticket from the host handoff, if provided.")
    snapshot_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$", description="Required unchanged snapshot_id when paging.")
    cursor: int = Field(default=0, ge=0, strict=True, description="Start at 0; then use next_cursor until null. Never infer absence from unread pages.")

    @model_validator(mode="after")
    def check_action(self):
        if self.action == "validate":
            if not self.proposal or not self.snapshot_id or self.cursor:
                raise ValueError("试修须提供 proposal、snapshot_id，不能使用分页游标")
            if len(encode(self.proposal)) > 100000:
                raise ValueError("试修方案超过有界输入上限")
            if self.ticket and self.ticket != self.proposal.get("ticket"):
                raise ValueError("试修票据不一致")
        elif self.proposal is not None:
            raise ValueError("只读分页不能附带试修方案，请使用 action=validate")
        return self


class InspectStageGraph(Tool):
    name = "InspectStageGraph"
    category = "read"
    consumes_runtime_slot = False
    is_concurrency_safe = True
    params_model = Params
    description = (
        "Read THIS conversation's current candidate stage graph (阶段图/候选图), including INVALID graphs. "
        "Exports all nodes, every edge/condition, meta.loops, runtime.semantics, full requirements and failure paths. "
        "Read every page with the same snapshot_id before concluding reachability or missing edges. "
        "A saved canvas or partial problem snippet is NOT the candidate. No history replay, file writes, "
        "graph adoption, saving or execution. After reading all pages, use action=validate with snapshot_id and "
        "proposal to trial a repair; fix errors before returning a mewcode-graph-repair for user preview/adoption."
    )

    def __init__(self, agent):
        self.agent = agent

    async def execute(self, params):
        agent = self.agent
        context = getattr(agent, "runtime_context", None)
        service = VALIDATOR_SERVICE if params.action == "validate" else READER_SERVICE
        reader = context.get(service, None) if context else None
        if not callable(reader):
            return ToolResult("当前运行没有宿主候选图读取服务；不能以局部摘要、旧画布或猜测替代。", is_error=True)
        project, cid = getattr(agent, "work_dir", ""), getattr(agent, "session_id", "")
        active = active_work_dir()
        if (not project or not cid or (active and os.path.normcase(os.path.abspath(active)) != os.path.normcase(os.path.abspath(project)))):
            return ToolResult("候选图读取的项目或对话绑定不匹配，未读取其他范围。", is_error=True)
        try:
            args = ({"proposal": params.proposal, "snapshot_id": params.snapshot_id} if params.action == "validate"
                    else params.model_dump(include={"ticket", "snapshot_id", "cursor"}))
            result = await reader(project=project, conversation_id=cid, **args)
            return ToolResult(encode(result))
        except (ValueError, OSError, sqlite3.Error) as exc:
            return ToolResult(f"候选图读取失败：{exc}", is_error=True)
