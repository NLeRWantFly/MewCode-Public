"""A task-independent, evidence-backed loop decision (not a free-form debate)."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult
from mewcode.tools.submit_verdict import VerdictHolder


class LoopCheck(BaseModel):
    criterion_id: str
    state: Literal["PASS", "PARTIAL", "FAIL", "UNKNOWN"]
    evidence: str = Field(min_length=1, max_length=2000, description="本轮核验的新证据，不是观点或承诺")
    location: str = Field(min_length=1, max_length=1000, description="证据位置：文件/测试记录/数据/上游节点输出")


class SubmitLoopDecisionParams(BaseModel):
    contract_hash: str = Field(description="当前宿主冻结循环上下文提供的合同哈希，原样复制；不能自行计算或猜测。")
    decision: Literal["EXIT", "CONTINUE", "BLOCKED"] = Field(description="EXIT=全部冻结条件 PASS；CONTINUE=本轮需返修；BLOCKED=当前无法继续。团队模式下 BLOCKED 必须给出用户问题。")
    checks: list[LoopCheck] = Field(min_length=1, max_length=32)
    question: str = Field(default="", max_length=2000, description="仅确需用户输入时填写具体问题")
    repair_node_ids: list[str] = Field(default_factory=list, max_length=32,
        description="需要补证据或修订的当前循环成员 ID；不必重跑无关成员")


class SubmitLoopDecisionTool(Tool):
    name = "SubmitLoopDecision"
    description = (
        "场景：循环审查节点提交本轮结构化结论。使用当前 contract_hash，checks 必须覆盖全部固定 criterion_id 各一次，"
        "每项给 state、实际 evidence 和 location。全部 PASS 才 EXIT；团队内可补齐的缺证据用 CONTINUE，"
        "repair_node_ids 仅列当前循环的执行成员；团队模式确需用户输入才 BLOCKED 并填写具体 question。"
        "非团队模式存在 UNKNOWN 时必须 BLOCKED。判定后调度模式遵循当前上下文声明的首次入口/返修节点集合。"
        "成功返回已提交的决策，由宿主决定后续调度；不授予权限或自动通过整项任务。"
        "合同或条件 ID 错误时按当前冻结上下文修正；成功后本轮不可覆盖，不要重复提交。")
    category = "read"
    params_model = SubmitLoopDecisionParams

    def __init__(self, holder: VerdictHolder, context: dict):
        self.holder = holder
        self.context = context
        self.completion_guard = None

    async def execute(self, params: SubmitLoopDecisionParams) -> ToolResult:
        if params.decision == "EXIT" and self.completion_guard is not None:
            try:
                blocker = self.completion_guard()
            except (ValueError, OSError) as exc:
                blocker = str(exc)
            if blocker:
                return ToolResult(output=blocker, is_error=True)
        expected = {item["criterion_id"] for item in self.context["criteria"]}
        if params.contract_hash != self.context["contract_hash"]:
            return ToolResult(output="合同编号不匹配；必须使用当前冻结合同。", is_error=True)
        if len(params.checks) != len(expected) or {item.criterion_id for item in params.checks} != expected:
            return ToolResult(output="必须且只能评价全部固定条件 ID，各一次。", is_error=True)
        if any(not item.evidence.strip() or not item.location.strip() for item in params.checks):
            return ToolResult(output="每项必须有非空证据和位置。", is_error=True)
        all_pass = all(item.state == "PASS" for item in params.checks)
        team = self.context.get("effective_control_mode", self.context.get("control_mode")) == "team"
        if (params.decision == "EXIT") != all_pass or (
            not team and any(item.state == "UNKNOWN" for item in params.checks) and params.decision != "BLOCKED"
        ):
            return ToolResult(output="决策与条件不一致：全部 PASS 才能 EXIT，UNKNOWN 必须 BLOCKED。", is_error=True)
        if team and params.decision == "BLOCKED" and not params.question.strip():
            return ToolResult(output="请写明需要用户回答的具体问题；团队内部可以补齐的证据请用 CONTINUE。", is_error=True)
        workers = set(self.context.get("node_ids") or []) - set(self.context.get("reviewer_node_ids") or [])
        if set(params.repair_node_ids) - workers:
            return ToolResult(output="repair_node_ids 只能指定当前循环的执行成员。", is_error=True)
        if (self.context.get("repair_dispatch") == "after_decision" and params.decision == "CONTINUE"
                and self.context.get("acting_node_id") == self.context.get("check_node_id")):
            entry = self.context["entry_node_id"]
            allowed = {entry, *self.context.get("repair_node_ids", [])}
            if (not params.repair_node_ids or set(params.repair_node_ids) - allowed
                    or len(set(params.repair_node_ids)) != len(params.repair_node_ids)
                    or entry in params.repair_node_ids and len(params.repair_node_ids) != 1):
                return ToolResult(output="判定后调度必须明确选择首次入口或已声明返修节点，不能混选。", is_error=True)
        if self.holder.read() is not None:
            return ToolResult(output="本轮已提交，不可覆盖判定。", is_error=True)
        self.holder.submit(params.model_dump())
        return ToolResult(output=f"判定已提交：{params.decision}，由看门狗决定是否继续。")
