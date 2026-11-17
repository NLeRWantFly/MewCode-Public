from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mewcode.tools.base import Tool, ToolResult

StepStatus = Literal["pending", "running", "completed", "failed"]


class WorkProgress(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    scope: str = Field(min_length=1, max_length=120,
        description="用户认识的工作范围，如书名；同一范围沿用相同名称，不把本批总量当整本书总量。")
    unit: str = Field(default='项', min_length=1, max_length=12, description="页、章、文件等业务单位。")
    current: int | None = Field(default=None, ge=1, le=10**12, strict=True, description="当前处理位置，如第5页；位置不等于完成数。")
    total: int | None = Field(default=None, ge=1, le=10**12, strict=True, description="同一范围的已知总量；未知时省略，不能估造。")
    completed: int | None = Field(default=None, ge=0, le=10**12, strict=True,
        description="已有依据的已处理数量；不表示质量验收通过，不能从当前位置或循环轮数推算。")

    @model_validator(mode='after')
    def consistent_counts(self):
        if self.current is None and self.completed is None:
            raise ValueError('progress 至少提供当前位置或已处理数量')
        if self.total is not None and any(value is not None and value > self.total
                                          for value in (self.current, self.completed)):
            raise ValueError('当前位置和已处理数量不能超过同范围总量')
        return self


class WorkIssue(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=100, description="同一问题沿用稳定标识；优先使用已有问题编号。")
    scope: str = Field(min_length=1, max_length=120, description="所属书名或工作范围，与 progress.scope 一致。")
    location: str = Field(min_length=1, max_length=160, description="用户可定位的位置，如第5页第二个公式。")
    message: str = Field(min_length=1, max_length=500, description="说明内容问题及影响，如公式缺少分母；不罗列代码、节点ID、哈希或内部异常。")
    state: Literal['open', 'fixing', 'resolved'] = Field(default='open',
        description="待处理、正在修复、已修正待复核；汇报状态不能替代独立验收。")
    next_step: str = Field(default='', max_length=300, description="正在采取或计划采取的业务处理动作。")


class ReportStepParams(BaseModel):
    label: str = Field(description="Short step label, e.g. 准备查找网络")
    status: StepStatus = "running"
    node: str = Field(
        default="",
        description=(
            "Canvas node this step belongs to, by id or name from the "
            "[MEWCODE_CANVAS_NODES] list. Required when that list exists; "
            "pass 无 when the work matches no listed node. Never leave it "
            "empty when a canvas exists."
        ),
    )
    summary: str = Field(
        default="",
        description="A concise first-person work report: what part I am working on, what changed, and what happens next. State only observable facts, not private reasoning.",
    )
    work_object: str = Field(default="", description="Concrete document section, file, or other work target.")
    purpose: str = Field(default="", description="User-visible purpose of this action, not internal deliberation.")
    findings: str = Field(default="", description="Findings supported by the work already performed.")
    changes: str = Field(default="", description="Actual changes made; distinguish planned changes from completed ones.")
    verification: str = Field(default="", description="Observed check result; tool completion is not acceptance.")
    blocker: str = Field(default="", description="What prevents progress or needs user input, if any.")
    next_step: str = Field(default="", description="Next intended action, explicitly a plan rather than a completed fact.")
    evidence_refs: list[str] = Field(default_factory=list, description="Existing activity/item IDs supporting this report, when available. Never invent IDs.")
    progress: WorkProgress | None = Field(default=None, description="按业务对象汇报数量进度，供画布显示如第5页/共110页。")
    issues: list[WorkIssue] = Field(default_factory=list, max_length=5,
        description="本次发现、正在修复或已处理的问题；使用同一scope/id更新，未变化的问题无需重复发送。")


REPORT_TEXT_FIELDS = ("summary", "work_object", "purpose", "findings", "changes", "verification", "blocker", "next_step")


def report_fields(value: Any) -> dict[str, Any]:
    """Bounded public report fields; never copy arbitrary model arguments."""
    if not isinstance(value, dict):
        return {}
    result = {key: value[key].strip()[:2000] for key in REPORT_TEXT_FIELDS
              if isinstance(value.get(key), str) and value[key].strip()}
    refs = value.get("evidence_refs")
    if isinstance(refs, list):
        result["evidence_refs"] = list(dict.fromkeys(
            ref[:200] for ref in refs if isinstance(ref, str) and ref.strip()))[:20]
        if not result["evidence_refs"]:
            result.pop("evidence_refs")
    return result


def format_step_output(label: str, status: StepStatus) -> str:
    return f"STEP\t{status}\t{label}"


def parse_step_output(output: str) -> tuple[str, StepStatus] | None:
    parts = output.split("\t", 2)
    if len(parts) != 3 or parts[0] != "STEP":
        return None
    status = parts[1]
    if status not in {"pending", "running", "completed", "failed"}:
        return None
    return parts[2], status  # type: ignore[return-value]


def build_expert_step_event(
    *,
    label: str,
    status: str,
    role: str,
    node_id: str,
) -> dict[str, Any]:
    return {
        "type": "expert_step",
        "label": label,
        "status": status,
        "role": role,
        "node_id": node_id,
    }


def expert_step_from_report(
    *,
    args: dict[str, Any] | None,
    output: str,
    role: str,
    node_id: str,
) -> dict[str, Any] | None:
    label = str((args or {}).get("label") or "").strip()
    status = str((args or {}).get("status") or "").strip()
    if not label:
        parsed = parse_step_output(output)
        if parsed is None:
            return None
        label, status = parsed
    elif not status:
        status = "running"
    event = build_expert_step_event(
        label=label, status=status, role=role, node_id=node_id
    )
    event.update(report_fields(args))
    if args:
        try:
            params = ReportStepParams.model_validate(args)
            if params.progress:
                event['progress'] = params.progress.model_dump(exclude_none=True)
            if params.issues:
                event['issues'] = [issue.model_dump() for issue in params.issues]
        except ValueError:
            pass
    return event


class ReportStepTool(Tool):
    name = "report_step"
    description = (
        "Report meaningful progress in natural first-person language using summary and optional detail fields. "
        "Report starts, findings, actual edits, check results, rework and blockers; merge repetitive reads/retries. "
        "Use a concrete action-and-object label, not tool names or task codes. Do not expose private reasoning or claim acceptance from tool success. "
        "Pass node=<画布节点名或 id> when the project has an orchestration "
        "canvas, so the canvas highlights that node and lists the step there; "
        "the step's files are recorded as process files automatically. "
        "Pass node=无 when no listed node matches."
        " For books, batches or repeated work, include progress with the actual scope, unit, current position and known total. "
        "Explain where the user's work is stuck and what a later review found using issues: e.g. 第5页的公式缺少分母，正在修正. "
        "Reuse issue IDs when fixing/resolving them. Do not show internal node IDs, hashes or exception codes as the explanation. "
        "A position, a processed count, a loop round and quality acceptance are distinct facts."
    )
    params_model = ReportStepParams
    category = "read"
    is_system_tool = True
    is_concurrency_safe = True
    consumes_runtime_slot = False

    async def execute(self, params: ReportStepParams) -> ToolResult:
        report = {'schema_version': 1, 'label': params.label.strip()[:180], 'status': params.status,
                  **report_fields(params.model_dump()),
                  'issues': [issue.model_dump() for issue in params.issues]}
        if params.progress:
            report['progress'] = params.progress.model_dump(exclude_none=True)
        return ToolResult(output=format_step_output(params.label, params.status),
                          host_observation={'work_report': report})
