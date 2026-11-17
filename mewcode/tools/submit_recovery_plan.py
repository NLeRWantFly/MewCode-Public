from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from mewcode.experts.authorization_ledger import valid_request
from mewcode.tools.base import Tool, ToolResult
from mewcode.experts.recovery_context import PROBE_TOOLS


INVESTIGATION_TOOLS = frozenset({
    'ReadFile', 'Glob', 'Grep', 'ReadCaseDossier', 'ProbeCapabilities',
    'ReadStageResult', 'ReadRecoveryContext', 'ReadAuthorizations', 'VerifyRecoveryCheckpoint',
})
READONLY_VERIFICATION_TOOLS = frozenset({
    'ReadFile', 'Glob', 'Grep', 'ReadAuditArtifact', 'InspectVisualEvidence',
    'ProbeCapabilities', 'VerifyRecoveryCheckpoint',
})


class Strategy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: str = Field(min_length=1, max_length=80)
    tools: list[str] = Field(max_length=32)
    target: str = Field(min_length=1, max_length=500)

    @model_validator(mode='after')
    def concrete_tools(self):
        if any(not name or '*' in name for name in self.tools) or len(self.tools) != len(set(self.tools)):
            raise ValueError('策略工具必须具体且不重复，不能使用通配符')
        if 'RequestAssistance' in self.tools:
            raise ValueError('案件执行员不能嵌套 RequestAssistance；请用实际执行工具修复，并用 VerifyRecoveryCheckpoint 核验宿主回执')
        return self


class RecoveryAuthority(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation_class: Literal['external_send', 'network', 'write_outside_workspace',
        'install_dependency', 'run_command_prefix', 'spend_over_cap', 'other']
    scope: dict = Field(description='A non-empty object, never a prose string. Allowed keys: operation_classes, tools, paths, hosts, model_routes, content_classes, cost_cap_tokens, node_ids, command_prefix, epoch, case_id, round, operation_digest. Collection values are concrete string arrays; case_id and operation_digest are strings; cost_cap_tokens, epoch and round are non-negative integers. external_send requires model_routes plus content_classes; network requires hosts; write_outside_workspace requires paths; run_command_prefix requires command_prefix.',
        examples=[{'tools': ['ReadFile'], 'paths': ['C:/project/input.pdf']}])
    covered_by: str | None = Field(default=None, description='Existing AUTH entry ID when this exact operation is already covered.')
    based_on_statement: str | None = Field(default=None, description='ST entry ID for an unconfirmed user statement; it is not permission.')

    @model_validator(mode='after')
    def supported_scope(self):
        valid_request(self.model_dump(exclude_none=True))
        return self


class RecoveryInputQuestion(BaseModel):
    """A candidate input field copied from the host's current contract.

    The analyst may suggest which already-declared fields should be shown on
    a recovery card.  This is intentionally a small structural model: the
    host still compares every field with the live run-input contract before
    accepting a plan, so this model is not a way to invent a new input or a
    new type.
    """

    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=120)
    label: str = Field(default='', max_length=100)
    description: str = Field(default='', max_length=500)
    type: Literal['text', 'multiline', 'list'] | None = None
    required: bool | None = None
    options: list[dict[str, Any]] = Field(default_factory=list, max_length=6)
    collect_at: str | None = Field(default=None, min_length=1, max_length=100)
    collect_at_label: str | None = Field(default=None, max_length=120)
    required_when: dict[str, str] | None = None
    default: str | list[str] | None = None
    separator: str | None = Field(default=None, max_length=20)

    @model_validator(mode='after')
    def valid_name_and_options(self):
        if self.name != self.name.strip() or '\x00' in self.name:
            raise ValueError('input_questions.name 无效')
        if self.label and self.label != self.label.strip():
            raise ValueError('input_questions.label 无效')
        if self.description and '\x00' in self.description:
            raise ValueError('input_questions.description 无效')
        if self.collect_at is not None and (self.collect_at != self.collect_at.strip() or '\x00' in self.collect_at):
            raise ValueError('input_questions.collect_at 无效')
        if self.collect_at_label is not None and '\x00' in self.collect_at_label:
            raise ValueError('input_questions.collect_at_label 无效')
        if self.type is not None and self.type != 'list' and self.separator is not None:
            raise ValueError('只有 list 输入可以声明 separator')
        seen: set[str] = set()
        for option in self.options:
            if not isinstance(option, dict) or not {'value', 'label'} <= set(option):
                raise ValueError('input_questions.options 必须含 value 和 label')
            if set(option) - {'value', 'label', 'description', 'recommended'}:
                raise ValueError('input_questions.options 字段无效')
            value = option.get('value')
            label = option.get('label')
            if (not isinstance(value, str) or not value or value != value.strip()
                    or '\x00' in value or not isinstance(label, str)
                    or not label.strip() or '\x00' in label):
                raise ValueError('input_questions.options 值无效')
            if value in seen:
                raise ValueError('input_questions.options 值不能重复')
            seen.add(value)
            if type(option.get('recommended', False)) is not bool:
                raise ValueError('input_questions.options.recommended 必须为布尔值')
        if sum(bool(item.get('recommended', False)) for item in self.options) > 1:
            raise ValueError('input_questions.options 最多一个推荐项')
        if self.required_when is not None and set(self.required_when) != {'input', 'equals'}:
            raise ValueError('input_questions.required_when 字段无效')
        return self


# Short compatibility name for callers that model a recovery card directly.
InputQuestion = RecoveryInputQuestion


class Needs(BaseModel):
    model_config = ConfigDict(extra='forbid')
    capabilities: list[str] = Field(default_factory=list, max_length=32)
    authority: list['RecoveryAuthority'] = Field(default_factory=list, max_length=16,
        description='Only permissions needed to execute this option. Read-only analysis and already approved retry rounds need no new permission request.')
    user_input: list[str] = Field(default_factory=list, max_length=16,
        description='Self-contained questions for missing facts or business choices, not duplicate permission confirmations. State exact materials and the information requested; do not refer to unspecified above scope or internal AUTH/ST IDs. Put typed operation approvals in authority.')
    input_questions: list[RecoveryInputQuestion] = Field(default_factory=list, max_length=16,
        description='Optional typed candidates copied from the current run-input contract. The host rejects unknown, future, or type-mismatched fields.')

    @model_validator(mode='after')
    def typed(self):
        for item in self.authority:
            valid_request(item.model_dump(exclude_none=True))
        names = [item.name for item in self.input_questions]
        if len(names) != len(set(names)):
            raise ValueError('input_questions 的字段不能重复')
        return self


class Verification(BaseModel):
    model_config = ConfigDict(extra='forbid')
    probe: str = Field(min_length=1, max_length=500)
    expected_evidence: str = Field(min_length=1, max_length=500)
    tool: str = Field(min_length=1, description='Supported host probes: ' + ', '.join(PROBE_TOOLS) + '. Prefer VerifyRecoveryCheckpoint for an existing successful observation.')
    arguments: dict

    @model_validator(mode='after')
    def executable(self):
        if self.tool not in PROBE_TOOLS:
            raise ValueError('宿主不支持该 verification.tool；可用：' + ', '.join(PROBE_TOOLS))
        if self.tool == 'VerifyRecoveryCheckpoint':
            from mewcode.tools.verify_recovery_checkpoint import VerifyRecoveryCheckpointParams
            model = VerifyRecoveryCheckpointParams
        elif self.tool == 'capability_binding':
            return self
        else:
            from mewcode.tools.read_file import ReadFile
            from mewcode.tools.glob import Glob
            from mewcode.tools.grep import Grep
            from mewcode.tools.read_audit_artifact import ReadAuditArtifact
            from mewcode.tools.inspect_visual_evidence import InspectVisualEvidence
            from mewcode.tools.capabilities import ProbeCapabilities
            model = {'ReadFile': ReadFile, 'Glob': Glob, 'Grep': Grep,
                'ReadAuditArtifact': ReadAuditArtifact, 'InspectVisualEvidence': InspectVisualEvidence,
                'ProbeCapabilities': ProbeCapabilities}[self.tool].params_model
        unknown = set(self.arguments) - set(model.model_fields)
        if unknown:
            raise ValueError('核验参数不属于 ' + self.tool + '：' + ', '.join(sorted(unknown)))
        params = model(**self.arguments)
        if self.tool == 'ReadAuditArtifact' and (params.action != 'read' or not params.file_path.strip()):
            raise ValueError('ReadAuditArtifact 核验必须读取具体 file_path；它不是命令审计查询工具。命令回执使用 VerifyRecoveryCheckpoint')
        return self


class DiagnosisSymptom(BaseModel):
    model_config = ConfigDict(extra='forbid')
    summary: str = Field(min_length=1, max_length=500)
    excerpt: str = Field(min_length=1, max_length=4000)
    evidence_id: str | None = Field(default=None, min_length=1, max_length=120)


class DiagnosisEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=120)
    excerpt: str = Field(min_length=1, max_length=4000)


class DiagnosisHypothesis(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=500)
    confidence: Literal['low', 'medium', 'high']
    evidence: list[DiagnosisEvidence] = Field(min_length=1, max_length=8)
    open_questions: list[str] = Field(default_factory=list, max_length=8)
    option_id: str = Field(min_length=1, max_length=80)
    delta: Literal['已排除', '已确认', '把握升高', '把握降低', '新增', '无变化'] | None = None


class Investigation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scope: str = Field(min_length=1, max_length=500)
    tools: list[str] = Field(min_length=1, max_length=16)
    budget: int = Field(ge=0, le=86400, description='Expected read-only investigation time in seconds.')

    @model_validator(mode='after')
    def readonly_tools(self):
        if len(self.tools) != len(set(self.tools)) or not set(self.tools) <= INVESTIGATION_TOOLS:
            raise ValueError('探查工具必须是具体的只读工具')
        return self


class GoalMilestone(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=300)
    evidence: str = Field(min_length=1, max_length=500)
    expected_seconds: int = Field(ge=0, le=604800)


class RecoveryGoal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target: str = Field(min_length=1, max_length=500)
    milestones: list[GoalMilestone] = Field(min_length=1, max_length=12)
    stop_when: str = Field(min_length=1, max_length=500)


class RecoveryDiagnosis(BaseModel):
    model_config = ConfigDict(extra='forbid')
    symptom: DiagnosisSymptom
    impact: str = Field(min_length=1, max_length=1000)
    tried: list[str] = Field(default_factory=list, max_length=16)
    hypotheses: list[DiagnosisHypothesis] = Field(max_length=3)
    investigation: Investigation | None = None
    goal: RecoveryGoal | None = None


class BoundaryReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['continue', 'converge']
    reason: str = Field(min_length=1, max_length=1000)
    next_step: str = Field(min_length=1, max_length=500)
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)


class RecoveryOption(BaseModel):
    model_config = ConfigDict(extra='forbid')
    option_id: str = Field(min_length=1, max_length=80)
    method: str = Field(min_length=1, max_length=300)
    strategy: Strategy
    hypothesis_id: str | None = Field(default=None, min_length=1, max_length=80)
    actor: Literal['auto', 'authorize', 'decide', 'provide'] | None = None
    consequence: str | None = Field(default=None, max_length=500)
    user_label: str | None = Field(default=None, max_length=120)
    needs: Needs = Field(default_factory=Needs)
    risk: Literal['low', 'medium', 'high']
    reversible: bool
    changes_acceptance: bool
    verification: Verification

    @model_validator(mode='after')
    def investigation_is_readonly(self):
        if self.strategy.kind == 'investigate':
            if (not self.strategy.tools or not set(self.strategy.tools) <= INVESTIGATION_TOOLS
                    or self.changes_acceptance
                    or self.verification.tool not in READONLY_VERIFICATION_TOOLS):
                raise ValueError('探查方案只能使用只读工具且不能改变验收')
        return self


class RootCause(BaseModel):
    model_config = ConfigDict(extra='forbid')
    class_: Literal['transient', 'environment_fixable', 'capability_bindable', 'capability_absent',
        'authority_gap', 'input_defect', 'contract_defect', 'business_decision', 'unknown_effect', 'unclassified'] = Field(alias='class',
        description='contract_defect requires an active host blocker of kind contract_defect or graph_invalid; a failed plan submission alone is not a business contract defect. capability_absent requires a complete host capability inventory with no eligible vision route.')
    summary: str = Field(min_length=1, max_length=400)
    evidence_refs: list[str] = Field(min_length=1, max_length=32,
        description='Use only IDs from root_evidence_refs in the host prompt: dossier:<this case ID> or active blocker fact IDs. Historical receipt IDs are not active root-cause evidence.')
    confidence: Literal['low', 'medium', 'high']


class SubmitRecoveryPlanParams(BaseModel):
    model_config = ConfigDict(extra='forbid')
    case_id: str
    round: int = Field(ge=1)
    root_cause: RootCause
    options: list[RecoveryOption] = Field(min_length=1, max_length=5)
    recommended_option_id: str
    diagnosis: RecoveryDiagnosis | None = None
    boundary_review: BoundaryReview | None = None
    not_recommended: list[dict] = Field(default_factory=list, max_length=16)

    @model_validator(mode='after')
    def unique(self):
        ids = [o.option_id for o in self.options]
        if len(ids) != len(set(ids)) or self.recommended_option_id not in ids:
            raise ValueError('方案编号必须唯一，推荐方案必须存在')
        return self


class SubmitRecoveryPlan(Tool):
    name = 'SubmitRecoveryPlan'
    description = 'Submit 1–5 ranked recovery options for this case and round. The host validates scoped references, authority, available executor tools and executable probe parameters.'
    category = 'read'
    params_model = SubmitRecoveryPlanParams
    consumes_runtime_slot = False

    def __init__(self, holder, case_id, round):
        self.holder, self.case_id, self.round = holder, case_id, round

    async def execute(self, params):
        if params.case_id != self.case_id or params.round != self.round:
            return ToolResult('案件或轮次不匹配', is_error=True)
        if self.holder:
            return ToolResult('本次派发已经提交方案', is_error=True)
        self.holder.update(params.model_dump(by_alias=True))
        return ToolResult('恢复方案已提交，等待宿主核验。')
