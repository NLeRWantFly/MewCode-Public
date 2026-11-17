"""Structured verdict submission for the auditor role.

The auditor ends its run with a structured tool call instead of free text, so
the orchestrator never parses VERDICT markers out of prose again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
import copy
import json

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult
from mewcode.experts.audit_protocol import AuditAssessment, validate_assessment

VERDICT_PASS = "PASS"
VERDICT_NEEDS_ATTENTION = "NEEDS_ATTENTION"


class ReviewDecisionOption(BaseModel):
    value: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=600)
    effect: Literal['review', 'request_revision', 'defer'] = Field(
        description='答复后重新核验、转入正式要求修订、保留暂停。任何选项都不直接放行或授予工具权限。')


class ReviewDecision(BaseModel):
    question: str = Field(min_length=1, max_length=1200)
    scope: str = Field(min_length=1, max_length=1600,
        description='精确的当前阶段、页面/对象、证据差异及未覆盖范围；不得把后续阶段授权提前当作当前阶段前提。')
    options: list[ReviewDecisionOption] = Field(min_length=2, max_length=4)


class VerdictIssue(BaseModel):
    issue_id: str = Field(
        default="",
        description=(
            "稳定问题编号；复审时同一问题必须复用原编号，例如 GT-01"
        ),
    )
    file: str = Field(default="", description="问题所在文件或范围")
    node_id: str = Field(default="", description="集中复核时填写产物所属的现有节点 ID，用于把修订直接交回生产者")
    description: str = Field(description="有证据的问题描述")
    criterion_id: str = Field(default="", description="所属冻结验收项编号")
    acceptance_test: str = Field(default="", description="关闭该问题必须满足的客观条件")
    origin: Literal["preexisting", "introduced", "out_of_scope"] = "preexisting"
    resolution: Literal['producer_fix', 'user_decision', 'capability_gap', 'downstream', 'unclassified'] = Field(
        default='unclassified', description='先判定由谁解决：当前生产者可修、用户决定、宿主/服务商能力缺口、后续阶段、尚不明确。不得仅按提到的文件派返工。')
    blocking: bool = Field(default=True, description='是否阻断当前阶段；冻结验收必需项不得标为非阻断。')
    target_node_id: str = Field(default='', description='后续阶段待办的现有目标节点 ID；不是返工生产者。')
    decision: ReviewDecision | None = Field(default=None,
        description='阻断的 user_decision 必填：宿主会展示问题与选项并保存答复，再核验本审核。先核对已有答复及要求，只问确实未覆盖的决定。能力缺口有明确业务选择时也可填写。')


class VerdictEvidence(BaseModel):
    kind: str = Field(default="test", description="证据类型：test / diff / read")
    command: str = Field(default="", description="产生证据的命令")
    exit_code: int | None = Field(default=None, description="命令退出码")
    summary: str = Field(default="", description="证据摘要")


class ReviewedDecision(BaseModel):
    applicability: Literal['original_scope_only', 'scope_changed', 'historical_scope_unverified'] = Field(
        description='原范围内沿用、范围已变化、旧记录范围仍待核对；宿主已保存的答复不能记为不存在。原范围沿用不授权其他模型或全量。')
    reason: str = Field(min_length=1, max_length=2000, description='依据原问题与当前输入说明适用范围；明确哪些范围仍未得到用户回答。')


class SubmitVerdictParams(BaseModel):
    verdict: Literal["PASS", "NEEDS_ATTENTION"] = Field(description="审计结论")
    issues: list[VerdictIssue] = Field(
        default_factory=list,
        description=(
            "NEEDS_ATTENTION 时必填：带稳定 issue_id 和证据的问题清单；"
            "复审必须沿用未解决问题的原 issue_id"
        ),
    )
    evidence: list[VerdictEvidence] = Field(
        default_factory=list, description="支撑结论的证据"
    )
    assessment: AuditAssessment | None = Field(
        default=None, description="存在冻结检查表时必填；复审同时提交 checks 与 baseline_checks"
    )
    reviewed_snapshots: dict[str, str] = Field(default_factory=dict, max_length=64,
        description="终校 PASS 必须引用已核验并关闭全部问题的上游审查快照：node_id -> snapshot_hash。未关闭时提交 NEEDS_ATTENTION。")
    reviewed_decisions: dict[str, ReviewedDecision] = Field(default_factory=dict, max_length=64,
        description='按宿主提供的 decision_id 逐项核对本节点及直接输入已有的用户答复。先用 ReadAuthorizations 读取原问题、答复和 freshness，再说明适用范围；不能遗漏已有答复或把它扩为权限。')
    reviewed_sources: dict[str, str] = Field(default_factory=dict, max_length=16,
        description='宿主要求的审查来源：ReadStageResult 的 source_id -> SOURCE_REVISION。须核对冻结要求及宿主模型回执概览，不能仅沿用旧报告。')


@dataclass
class VerdictHolder:
    """Collects the auditor's structured verdict; the orchestrator reads it."""

    value: dict[str, Any] | None = None
    submissions: list[dict[str, Any]] = field(default_factory=list)

    def submit(self, verdict: dict[str, Any]) -> None:
        self.value = verdict
        self.submissions.append(verdict)

    def read(self) -> dict[str, Any] | None:
        return self.value


class SubmitVerdictTool(Tool):
    """Submit the structured audit verdict exactly once, at the end."""

    name = "SubmitVerdict"
    description = (
        "Submit the structured audit verdict. Call this exactly once at the "
        "end of the audit: PASS, or NEEDS_ATTENTION with an evidence-backed "
        "issue list. Give every issue a stable issue_id and reuse that id on "
        "rechecks until the issue is resolved."
        " Classify each issue with resolution and blocking: producer_fix requires a concrete repair within the producer's existing capabilities; "
        "user_decision and capability_gap must not be sent back as artifact edits. "
        "For nonblocking work belonging to a later stage use downstream, blocking=false, origin=out_of_scope and an existing target_node_id. "
        "Such follow-ups do not fail the current stage if all its frozen criteria pass; never waive a required current criterion. "
        " Use only in the auditor's current scope after reading actual artifacts/upstream evidence. "
        "When a frozen contract exists, assessment must cover its checks and baseline checks; final PASS must cite required reviewed_snapshots. "
        "assessment.checks and baseline_checks contain objects with criterion_id, state (PASS/PARTIAL/FAIL/UNKNOWN), evidence and location; never booleans. "
        "Account for host-recorded user answers in reviewed_decisions, including historical answers whose current scope is unverified. "
        "Success records a verdict for the host; it neither edits artifacts nor grants permission. "
        "A rejected call is not a PASS: correct the reported missing criteria, snapshots or evidence and resubmit. "
        "Do not substitute SubmitHandoff or free-text approval for this structured verdict."
    )
    params_model = SubmitVerdictParams
    category = "read"
    is_concurrency_safe = False

    def __init__(self, holder: VerdictHolder | None = None, contract: dict | None = None,
                 *, required_reviews: dict[str, str] | None = None) -> None:
        self._holder = holder or VerdictHolder()
        self._contract = contract
        self._required_reviews = dict(required_reviews or {})
        self.required_decisions: dict[str, dict] = {}
        self.required_sources: dict[str, str] = {}
        self.completion_guard = None

    inline_schema_refs = True

    def _reject(self, category, code, message, *, waiting_for='agent'):
        return ToolResult(message, is_error=True, host_observation={'submission': {
            'schema_version': 1, 'tool': self.name, 'status': 'rejected', 'category': category,
            'code': code, 'next_action': message, 'waiting_for': waiting_for}})

    async def execute(self, params: SubmitVerdictParams) -> ToolResult:
        result = await self._submit(params)
        observation = dict(result.host_observation or {})
        observation.setdefault('submission', {
            'schema_version': 1, 'tool': self.name,
            'status': 'rejected' if result.is_error else 'accepted',
            'category': 'contract_error' if result.is_error else 'submitted',
            'code': 'submission_contract_error' if result.is_error else 'submission_accepted',
            'next_action': result.output if result.is_error else '等待宿主验收；提交成功本身不代表验收通过。',
            'waiting_for': 'agent' if result.is_error else 'host',
        })
        result.host_observation = observation
        return result

    def reference_rejections(self, params: SubmitVerdictParams) -> list[ToolResult]:
        """Inspect current reference requirements without submitting or clearing a verdict."""
        rejected = []
        mismatched = {key: revision for key, revision in self.required_sources.items()
                      if params.reviewed_sources.get(key) != revision}
        if mismatched:
            result = self._reject('reference_mismatch', 'submission_source_revision_mismatch',
                '当前 state 要求修正以下 reviewed_sources 引用。已有相同 source_revision 的完整核验结果直接复用；'
                '仅缺少该版本证据时，用 ReadStageResult 定向读取对应来源，无需重读其他已核验来源。\n'
                + json.dumps(mismatched, ensure_ascii=False))
            result.host_observation['submission']['mismatched_sources'] = mismatched
            rejected.append(result)
        missing = set(self.required_decisions) - set(params.reviewed_decisions)
        unknown = set(params.reviewed_decisions) - set(self.required_decisions)
        if missing or unknown:
            details = {'required_decision_ids': sorted(self.required_decisions),
                       'missing_decision_ids': sorted(missing), 'unknown_decision_ids': sorted(unknown)}
            if missing:
                details['required_decisions'] = {key: self.required_decisions[key] for key in sorted(missing)}
            action = ('删除 reviewed_decisions 中不属于本次要求的 unknown_decision_ids。' if unknown else '')
            if missing:
                action += ('仅补充 missing_decision_ids 的适用范围；当前 state 中已有的原问题与答复直接复用，'
                           '缺少具体内容时才用 ReadAuthorizations(decision_id=...) 定向读取。')
            if not self.required_decisions:
                action += '本次必需答复集合为空，reviewed_decisions 应为 {}。'
            action += '这是提交引用不匹配，不代表用户尚未回答；保留审计草稿和实际证据，纠正字段后重交。'
            rejected.append(ToolResult(output=action + '\n' + json.dumps(details, ensure_ascii=False), is_error=True,
                host_observation={'submission': {'schema_version': 1, 'tool': self.name,
                    'status': 'rejected', 'category': 'reference_mismatch',
                    'code': 'submission_reference_mismatch', 'waiting_for': 'agent',
                    'next_action': action, **details}}))
        return rejected

    async def _submit(self, params: SubmitVerdictParams) -> ToolResult:
        # A rejected replacement must not leave a previous PASS as the final
        # submitted result. Preserve the submissions list as historical evidence.
        self._holder.value = None
        rejected = self.reference_rejections(params)
        if rejected:
            return rejected[0]
        if any(issue.resolution == 'unclassified' for issue in params.issues):
            return ToolResult(output='请先为每个问题填写 resolution：producer_fix（生产者可修）、user_decision（用户决定）、'
                'capability_gap（宿主或服务商能力缺口）、downstream（后续阶段）。同时明确 blocking；不能仅按文件分派返工。', is_error=True)
        for issue in params.issues:
            if issue.blocking and issue.resolution == 'user_decision' and issue.decision is None:
                return ToolResult(output='阻断的 user_decision 需要 decision.question、scope 及 2–4 个明确选项，'
                    '宿主才能展示并处理答复。已有方法不重复征求同意，后续阶段授权不提前当作当前验收条件。', is_error=True)
            if issue.decision is not None:
                options = issue.decision.options
                if (not issue.issue_id.strip() or issue.resolution not in {'user_decision', 'capability_gap'}
                        or len({o.value for o in options}) != len(options)
                        or not any(o.effect == 'defer' for o in options)):
                    return ToolResult(output='审查决定需要稳定 issue_id、唯一选项值及保留暂停的选项，'
                        '且只能属于 user_decision 或 capability_gap。', is_error=True)
        if params.verdict == 'PASS' and self.completion_guard is not None:
            try:
                blocker = self.completion_guard()
            except (ValueError, OSError) as exc:
                return self._reject('service_fault', 'submission_ledger_unavailable',
                    f'审阅台账核验失败，保留草稿并由宿主检查台账后重试，不能提交 PASS：{exc}', waiting_for='host')
            if blocker:
                return self._reject('evidence_missing', 'submission_review_incomplete',
                    f'审阅台账未通过，先完成尚缺的核验；不能提交 PASS：{blocker}')
        value: dict[str, Any] = {
            "verdict": params.verdict,
            "issues": [issue.model_dump() for issue in params.issues],
            "evidence": [item.model_dump() for item in params.evidence],
        }
        if params.verdict == 'PASS' and any(issue.blocking or issue.origin != 'out_of_scope' for issue in params.issues):
            return self._reject('quality_failure', 'submission_quality_conflict',
                '仍有当前阶段阻断项，应如实提交 NEEDS_ATTENTION；后续非阻断待办须明确标记 blocking=false、origin=out_of_scope。')
        if params.assessment is not None:
            value["assessment"] = params.assessment.model_dump()
        if params.reviewed_snapshots:
            value["reviewed_snapshots"] = dict(params.reviewed_snapshots)
        if params.reviewed_sources:
            value['reviewed_sources'] = dict(params.reviewed_sources)
        if params.reviewed_decisions:
            value['reviewed_decisions'] = {key: {'assessment': item.model_dump(),
                'host_record': copy.deepcopy(self.required_decisions[key])}
                for key, item in params.reviewed_decisions.items()}
        if params.verdict == "PASS" and any(params.reviewed_snapshots.get(key) != digest
                                            for key, digest in self._required_reviews.items()):
            return self._reject('reference_mismatch', 'submission_snapshot_mismatch',
                "不能提交 PASS：尚未核验并引用本轮上游冻结审查快照。"
                "请逐项核对其问题；未关闭时提交 NEEDS_ATTENTION。required reviewed_snapshots="
                + str(self._required_reviews))
        if self._contract:
            try:
                validate_assessment(
                    self._contract["rubric"], value, self._contract["artifact_hash"],
                    self._contract.get("baseline_hash", ""),
                )
            except ValueError as exc:
                return ToolResult(output=f"验收协议不完整：{exc}", is_error=True)
        self._holder.submit(value)
        return ToolResult(
            output=(
                f"结论已提交：{params.verdict}"
                f"（{len(params.issues)} 个问题，{len(params.evidence)} 条证据）"
            )
        )
