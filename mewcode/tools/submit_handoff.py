"""Structured inter-stage context, independent of the user-facing report."""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from mewcode.tools.base import Tool, ToolResult
from mewcode.tools.ask_user import QuestionItem, QuestionOption
from mewcode.kernel.blockers import Blocker


class HandoffArtifact(BaseModel):
    path: str = Field(max_length=2000)
    kind: str = Field(default="file", max_length=100)
    note: str = Field(default="", max_length=2000)


class HandoffEvidence(BaseModel):
    kind: Literal["test", "command", "file", "web"] = "file"
    ref: str = Field(max_length=2000)
    result: str = Field(default="", max_length=4000)


class InlineOutput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=20000)

    @model_validator(mode="after")
    def nonblank(self):
        if not self.name.strip() or not self.content.strip():
            raise ValueError("内联交付名称和内容不能为空白")
        return self


class FileDeliveryRequest(BaseModel):
    output_name: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=2000)


class CommandExecutionRequest(BaseModel):
    command: str = Field(min_length=1, max_length=8000,
        description="由 executor 原样执行的单条只读命令；不得用占位符或让 executor 自行扩大范围。")
    purpose: str = Field(min_length=1, max_length=1000)
    input_scope: list[str] = Field(min_length=1, max_length=16,
        description="命令可读取的工作区路径或系统信息范围。")
    forbidden_changes: list[str] = Field(min_length=1, max_length=16,
        description="明确禁止的文件、配置、网络或外部状态变更。")
    deliverables: list[str] = Field(min_length=1, max_length=16,
        description="必须回传给原节点的输出与证据。")
    verification_command: str = Field(min_length=1, max_length=8000,
        description="用于验收本次探测的只读命令；必须是 command 本身，避免隐式扩大命令范围。")

    @model_validator(mode="after")
    def bounded_readonly_request(self):
        if not self.command.strip() or self.command.strip() != self.verification_command.strip():
            raise ValueError("命令转派的 verification_command 必须与待执行只读命令完全一致")
        for values in (self.input_scope, self.forbidden_changes, self.deliverables):
            if any(not str(item).strip() for item in values):
                raise ValueError("命令转派的范围、禁止项和交付项不能为空白")
        return self


class UnmetCheck(BaseModel):
    check_id: str = Field(min_length=1, max_length=100, description="当前宿主 handoff_policy 中的检查 ID。")
    version: int = Field(ge=1, strict=True, description="当前检查版本，不能引用旧版本或自行创建检查。")
    reason: str = Field(min_length=1, max_length=2000, description="尚未验证或未满足的事实及现有证据边界。")


class SubmitHandoffParams(BaseModel):
    summary: str = Field(max_length=2000, description="本节点已做什么、交付什么及当前结果的短摘要；完整分析放 inline_outputs，不能只写完成承诺。")
    status: Literal["completed", "ready_for_review", "blocked", "needs_attention"] = Field(default="completed",
        description="本段交付状态，不是最终验收结论。已有可审阅产物、仅剩可延后检查或证明缺口时用 ready_for_review 并填写 unmet_checks；缺少交接必需输入、授权或执行能力时才 blocked/needs_attention。")
    unmet_checks: list[UnmetCheck] = Field(default_factory=list, max_length=100,
        description="引用当前检查 ID、version 和具体缺口。ready_for_review 仅可引用 handoff_policy.may_defer_to_review；insufficient_evidence 阻塞须引用实际未满足的交接前置。缺证明不等于模型不可用。")
    blockers: list[str] = Field(default_factory=list, max_length=32,
        description="只列阻止实际执行或下游继续的主问题，并说明受影响的工作与缺口；可后补的质量问题放 open_items。")
    artifacts: list[HandoffArtifact] = Field(default_factory=list, max_length=32)
    decisions: list[str] = Field(default_factory=list, max_length=32)
    open_items: list[str] = Field(default_factory=list, max_length=32,
        description="保留未完成项；不影响下游的措辞、排版、优化和非必需材料可留待修订，不应改成 blockers 或用户问题。尚未生成的可选文件放这里，不能声明为已交付 artifacts。")
    evidence: list[HandoffEvidence] = Field(default_factory=list, max_length=16)
    inline_outputs: list[InlineOutput] = Field(default_factory=list, max_length=32,
        description="分析表、方案、建议等内容直接交付于此；宿主保存并供下游读取。不必另建文件，但不能替代明确要求的文件交付。")
    blocker_kind: Blocker = Field(default=Blocker.NONE,
        description='仅分段时间用尽、当前节点还有已授权工作时用 internal_work：status=needs_attention，未完成步骤放 open_items，blockers=[]、question=""；不能把软截止或内部待办写成真实阻塞。')
    question: str = Field(default="", max_length=2000, description="仅确需用户决策/授权/提供材料时填写具体问题；内部交付格式问题不是用户问题。")
    question_options: list[str | QuestionOption] = Field(default_factory=list,
        description="具体业务问题的可选方案：稳定 value、简短 label、后果 description 和推荐标记。不用技术恢复日志代替用户选项。")
    question_context: str = Field(default="", description="解释该问题所需的背景；不重复 question。")
    file_requests: list[FileDeliveryRequest] = Field(default_factory=list, max_length=32,
        description="仅当剩余工作全部是这些文件的落盘时使用。向上级请求 executor 将指定 inline_outputs 正文原样写入任务要求的相对路径；不是已完成的 artifacts，不得扩大授权范围。")
    command_requests: list[CommandExecutionRequest] = Field(default_factory=list, max_length=8,
        description="角色缺少可用命令能力时使用，包括 executor 的隔离执行门禁拒绝。每项须给出一条确定的只读命令、输入范围、禁止变更、交付结果和同一条验收命令；辅助 executor 只能执行这些命令并回传宿主观察，门禁拒绝不是测试失败。")
    method_id: str = Field(default="", max_length=100, description="协作合同指定的方法ID；不构成换路许可。")
    method_decision: str = Field(default="", max_length=100, description="变更指定方法时，引用宿主已批准的新合同决定。")
    contract_conflict: str = Field(default="", max_length=4000, description="合同方法或要求无法满足时说明证据，不能自行改合同。")

    @model_validator(mode="after")
    def bound_inline_content(self):
        if self.contract_conflict and (self.status in {"completed", "ready_for_review"} or self.blocker_kind != "contract_conflict"):
            raise ValueError("合同冲突必须提交未完成状态及 contract_conflict 阻塞，不能自认已换路完成")
        if self.question_options or self.question_context:
            if not self.question.strip():
                raise ValueError("用户选项和背景必须关联具体 question")
            QuestionItem(type="radio", name="direction", message=self.question, options=self.question_options)
        if self.blocker_kind == "internal_work":
            if (self.status != "needs_attention" or not self.open_items or self.blockers
                    or self.question or self.file_requests or self.command_requests):
                raise ValueError('internal_work 只表示本节点仍有已授权待办：须 needs_attention 和 open_items，不能含真实阻塞、用户问题或转派请求。'
                    '修正格式：{"status":"needs_attention","blocker_kind":"internal_work",'
                    '"open_items":["具体的下一项未完成工作"],"blockers":[],"question":""}；'
                    '保留原 summary、产物与证据，未完成不得改成 completed；若确有授权或依赖阻塞，改用其真实类型。')
        if sum(len(item.content) for item in self.inline_outputs) > 128000:
            raise ValueError("内联交付总内容不能超过 128000 字符；请拆分明确的交付项")
        if self.file_requests or self.command_requests:
            if self.file_requests and self.command_requests:
                raise ValueError("命令探测结果须先回传原节点再生成最终文件，不能与文件落盘请求混在同一次转派")
            names = [item.name for item in self.inline_outputs]
            if self.status in {"completed", "ready_for_review"} or self.blocker_kind != "capability_missing" or self.question:
                raise ValueError("转派请求须声明 capability_missing 和未完成状态；用户授权问题不能自动转派")
            if self.file_requests and any(names.count(item.output_name) != 1 for item in self.file_requests):
                raise ValueError("每个文件请求须引用唯一且完整的 inline_outputs 名称")
            commands = [item.command.strip() for item in self.command_requests]
            if len(commands) != len(set(commands)):
                raise ValueError("同一次命令转派不能重复执行相同命令")
        return self


@dataclass
class HandoffHolder:
    value: dict | None = None

    def read(self):
        return self.value


class SubmitHandoff(Tool):
    name = "SubmitHandoff"
    description = ("Submit stage handoff. Deliver analysis/tables/plans directly in inline_outputs; file artifacts are optional unless the task explicitly requires files. "
                   'Use at the end of a business node, or to checkpoint remaining work. Minimal content-only completion: '
                   '{"summary":"Analysis delivered","status":"completed","inline_outputs":[{"name":"Findings","content":"Full findings and evidence"}]}. '
                   "Success records this node's delivery for the host and downstream nodes; prose alone is not a handoff. "
                   "Delivery readiness is separate from quality acceptance. With a collaborative handoff_policy, use ready_for_review and versioned unmet_checks for usable drafts with deferred checks, including missing verification evidence. "
                   "Do not turn a deferred review into an execution blocker or claim it passed; insufficient_evidence blockers must reference a required-before-handoff check. "
                   "A rejected submission is not recorded as completion: correct the reported inconsistency or missing evidence and resubmit. "
                   "After RequestAssistance returns pending, yield to the host instead of submitting a completed handoff or repeating the request. "
                   "A read-only role can complete an analysis without writing files. Classify real blockers with blocker_kind; user_input requires a concrete question. "
                   "An unavailable required model or missing downstream input blocks handoff; harmless follow-ups belong in open_items and permit completed handoff. "
                   "For a business choice, include question_options with value, label, consequence/description and a recommendation; question_context supplies necessary background. Do not substitute recovery logs for choices. "
                   "Remaining authorized work in this node uses needs_attention + internal_work + open_items, not external_dependency. "
                   "For explicitly required files, submit capability_missing with file_requests and complete inline_outputs to request parent executor assistance. "
                   "When a role lacks usable command execution, including an executor blocked by isolation admission, use bounded command_requests instead of disguising a probe as a file request. "
                   "This does not approve acceptance or grant permissions.")
    params_model = SubmitHandoffParams
    inline_schema_refs = True
    category = "read"
    is_concurrency_safe = True

    def __init__(self, holder: HandoffHolder, *, work_dir=None, handoff_checks=None):
        self.holder = holder
        self.work_dir = work_dir
        import copy
        self.handoff_checks = copy.deepcopy(handoff_checks)

    async def execute(self, params: SubmitHandoffParams) -> ToolResult:
        value = params.model_dump()
        self.holder.value = None
        from mewcode.kernel.contracts import handoff_findings_error
        error = handoff_findings_error(value, self.handoff_checks)
        if error:
            return ToolResult(output=error, is_error=True)
        review_ready = value['status'] == 'ready_for_review'
        if review_ready:
            # Existing schedulers use completed for a submitted delivery, not
            # for quality acceptance. Retain the distinct readiness claim.
            value.update(status='completed', readiness='ready_for_review', acceptance_status='pending')
        if value["status"] == "completed" and (value["blocker_kind"] or value["question"]):
            return ToolResult(output="交接不一致：completed 不能同时声明阻塞类型或待回答问题。", is_error=True)
        if value["blocker_kind"] == "user_input" and not value["question"].strip():
            return ToolResult(output="请写出确实需要用户回答的 question；无需用户决定的交付形式问题用 delivery_format。", is_error=True)
        if value["blocker_kind"] == "delivery_format" and value["question"].strip():
            return ToolResult(output="交付形式纠正不能夹带用户问题；如需授权或决策请声明 user_input。", is_error=True)
        if value["status"] == "completed" and value["blockers"]:
            return ToolResult(output="交接不一致：仍有阻塞时必须提交 blocked 或 needs_attention。", is_error=True)
        if value["status"] == "completed":
            from mewcode.experts.completion import handoff_blocker
            try:
                blocker = await asyncio.to_thread(handoff_blocker, value, work_dir=self.work_dir)
            except OSError as exc:
                blocker = f"交付文件无法核验：{exc}"
            if blocker:
                return ToolResult(output=blocker + "；请修正产物声明或提交实际受阻状态。", is_error=True)
        for key in ("decisions", "open_items", "blockers"):
            value[key] = [str(item)[:2000] for item in value[key]]
        self.holder.value = value
        return ToolResult(output=("可审阅交接已记录，未满足检查随产物保留；质量仍待复核，未判验收通过。"
                                  if review_ready else "交接已记录；验收仍以结构化审计与实际证据为准。"))


def fallback_handoff(text, *, tools=None, verification_records=(), verdict=None):
    artifacts = {}
    for tool in (tools or {}).values():
        if tool.get("name") not in {"WriteFile", "EditFile"} or tool.get("status") != "completed":
            continue
        args = tool.get("args") or {}
        path = args.get("file_path") or args.get("path") or args.get("filePath")
        if path:
            artifacts[str(path)] = {"path": str(path), "kind": "file", "note": "本节点记录的文件操作"}
    evidence = [{"kind": "command", "ref": str((item.get("args") or {}).get("command") or item.get("tool_id") or item.get("name") or "")[:2000],
                 "result": str(item.get("status") or "") + ": " + str(item.get("output") or "")[:4000]} for item in verification_records][-16:]
    for tool in (tools or {}).values():
        if tool.get("status") != "completed" or tool.get("name") not in {
            "WebFetch", "web_fetch", "Browser", "browser", "browser_navigate", "browser_snapshot", "WebSearch", "web_search"
        }:
            continue
        args = tool.get("args") or {}
        ref = args.get("url") or args.get("query") or tool.get("name")
        evidence.append({"kind": "web", "ref": str(ref)[:2000],
                         "result": "Recorded tool output, not automatic factual acceptance: " + str(tool.get("output") or "")[:3000]})
    evidence = evidence[-16:]
    return {"summary": str(text)[:600], "artifacts": list(artifacts.values())[:32], "decisions": [],
            "open_items": [str(item.get("description") or item.get("issue_id") or "")[:2000]
                           for item in (verdict or {}).get("issues") or [] if isinstance(item, dict)][:32],
            "evidence": evidence}
