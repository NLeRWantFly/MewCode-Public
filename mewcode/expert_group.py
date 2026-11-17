from __future__ import annotations

import asyncio
import copy
import inspect
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from mewcode.errors import format_error
from mewcode.experts.delivery import handoff_error_kind, recovery_action
from mewcode.experts.supervision import supervision_prompt
from mewcode.experts.waits import BranchWaitPaused
from mewcode.experts.flow import (
    DEFAULT_EXPERT_FLOW,
    ExpertFlowSpec,
    ExpertMemberSpec,
    ExpertNodeSpec,
    edge_condition_matches,
    normalize_edge_condition,
)
from mewcode.model_registry import ModelRef, ProviderRegistry
from mewcode.runtime_paths import user_data_root


ExpertRole = str
ExpertStrategy = Literal["stable", "fast"]


def normalize_project_path(project_path: str | os.PathLike[str]) -> str:
    resolved = Path(project_path).expanduser().resolve()
    return os.path.normcase(str(resolved)).replace("\\", "/")


@dataclass(frozen=True)
class ExpertProfile:
    manager: ModelRef
    executor: ModelRef
    auditor: ModelRef
    planner: ModelRef | None = None
    capability_executor: ModelRef | None = None
    planner_routes: dict[str, tuple[ModelRef, ...]] = field(default_factory=dict)
    role_models: dict[str, ModelRef] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: Any) -> "ExpertProfile":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ValueError("expert profile must be an object")
        planner_value = value.get("planner")
        capability_executor_value = value.get("capability_executor")
        role_models = value.get('role_models') or {}
        if not isinstance(role_models, dict) or set(role_models) - {'planner', 'executor', 'verifier', 'reviewer', 'arbiter', 'coordinator'}:
            raise ValueError('模型角色绑定格式错误')
        raw_routes = value.get("planner_routes") or {}
        routes: dict[str, tuple[ModelRef, ...]] = {}
        if isinstance(raw_routes, dict):
            for kind in ("frontend", "backend", "other"):
                candidates = raw_routes.get(kind) or []
                if isinstance(candidates, list):
                    parsed = tuple(
                        ModelRef.from_value(candidate) for candidate in candidates[:12]
                    )
                    if parsed:
                        routes[kind] = parsed
        return cls(
            manager=ModelRef.from_value(value.get("manager")),
            executor=ModelRef.from_value(value.get("executor")),
            auditor=ModelRef.from_value(value.get("auditor")),
            planner=ModelRef.from_value(planner_value) if planner_value else None,
            capability_executor=(
                ModelRef.from_value(capability_executor_value)
                if capability_executor_value else None
            ),
            planner_routes=routes,
            role_models={role: ModelRef.from_value(model) for role, model in role_models.items()},
        )

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "manager": self.manager.as_dict(),
            "executor": self.executor.as_dict(),
            "auditor": self.auditor.as_dict(),
        }
        if self.planner is not None:
            result["planner"] = self.planner.as_dict()
        if self.capability_executor is not None:
            result["capability_executor"] = self.capability_executor.as_dict()
        if self.planner_routes:
            result["planner_routes"] = {
                kind: [model.as_dict() for model in models]
                for kind, models in self.planner_routes.items()
                if models
            }
        if self.role_models:
            result['role_models'] = {role: model.as_dict() for role, model in self.role_models.items()}
        return result


class ExpertProfileStore:
    """User-level project profile store; never writes into the project."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path or user_data_root() / "expert-profiles.json")

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def get(self, project_path: str | os.PathLike[str]) -> ExpertProfile | None:
        value = self._load().get(normalize_project_path(project_path))
        if not value:
            return None
        try:
            return ExpertProfile.from_value(value)
        except ValueError:
            return None

    def put(
        self,
        project_path: str | os.PathLike[str],
        profile: ExpertProfile | dict[str, Any],
    ) -> ExpertProfile:
        parsed = ExpertProfile.from_value(profile)
        data = self._load()
        data[normalize_project_path(project_path)] = parsed.as_dict()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.path)
        return parsed

    def set_capability_executor(
        self, project_path: str | os.PathLike[str], model: ModelRef | None,
    ) -> ExpertProfile:
        """Change only the project assistance slot, preserving every other field."""
        selected = ModelRef.from_value(model).as_dict() if model is not None else None
        data = self._load()
        key = normalize_project_path(project_path)
        current = data.get(key)
        if not isinstance(current, dict):
            raise ValueError("请先保存管理、执行和审计角色配置，再设置辅助执行器")
        changed = copy.deepcopy(current)
        if selected is None:
            changed.pop("capability_executor", None)
        else:
            changed["capability_executor"] = selected
        parsed = ExpertProfile.from_value(changed)
        data[key] = changed
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        return parsed


def relocate_expert_profile(
    registry: ProviderRegistry,
    profile: ExpertProfile,
) -> ExpertProfile:
    """Rewrite stale role bindings onto providers that still serve those models."""
    if not isinstance(profile, ExpertProfile):
        return profile
    if not hasattr(registry, "relocate_model_ref"):
        return profile

    def reloc(ref: ModelRef | None) -> ModelRef | None:
        if ref is None:
            return None
        return registry.relocate_model_ref(ref)

    routes = {
        kind: tuple(reloc(model) or model for model in models)
        for kind, models in profile.planner_routes.items()
        if models
    }
    return replace(
        profile,
        manager=reloc(profile.manager) or profile.manager,
        planner=reloc(profile.planner),
        executor=reloc(profile.executor) or profile.executor,
        auditor=reloc(profile.auditor) or profile.auditor,
        # Assistance is an explicit provider/model slot. A missing provider
        # must not be healed onto another provider without the user's choice.
        capability_executor=profile.capability_executor,
        planner_routes=routes,
    )


def classify_task(task: str) -> str:
    text = task.casefold()
    frontend = (
        "frontend", "front-end", "ui", "ux", "css", "html", "react",
        "vue", "svelte", "界面", "前端", "样式", "组件", "交互",
    )
    backend = (
        "backend", "back-end", "api", "database", "architecture", "security",
        "server", "fastapi", "django", "flask", "后端", "架构", "安全", "数据库",
    )
    if any(word in text for word in frontend):
        return "frontend"
    if any(word in text for word in backend):
        return "backend"
    return "other"


def _catalog_models(catalog: dict[str, Any]) -> list[tuple[ModelRef, dict[str, Any]]]:
    result: list[tuple[ModelRef, dict[str, Any]]] = []
    for details in catalog.get("models", []):
        if not isinstance(details, dict) or details.get("available") is False:
            continue
        provider_key = str(details.get("provider_key") or "")
        model_id = str(details.get("model_id") or details.get("id") or "").strip()
        capabilities = details.get("capabilities") or []
        if provider_key and model_id and (
            not capabilities or "chat" in capabilities or "planning" in capabilities
        ):
            result.append((ModelRef(provider_key, model_id, ""), details))
    for provider in catalog.get("providers", []):
        if not isinstance(provider, dict) or provider.get("available") is False:
            continue
        provider_key = str(provider.get("provider_key") or provider.get("key") or "")
        for raw_model in provider.get("models", []):
            details = raw_model if isinstance(raw_model, dict) else {"id": raw_model}
            model_id = str(details.get("id") or details.get("model_id") or "").strip()
            capabilities = details.get("capabilities") or []
            if (
                provider_key and model_id and details.get("available") is not False
                and (
                    not capabilities
                    or "chat" in capabilities
                    or "planning" in capabilities
                )
            ):
                result.append((ModelRef(provider_key, model_id, ""), details))
    return result


def select_planner_model(
    manager: ModelRef,
    catalog: dict[str, Any],
    task_kind: str,
    route_preferences: tuple[ModelRef, ...] | list[ModelRef] = (),
) -> tuple[ModelRef, str]:
    available = _catalog_models(catalog)
    keys = {(ref.provider_key, ref.model_id) for ref, _ in available}
    for candidate in route_preferences:
        if (candidate.provider_key, candidate.model_id) in keys:
            return candidate, "matched user planner route"
    preferences = ["opus5", "k3"] if task_kind == "backend" else ["k3", "opus5"]

    def matches(model_id: str, preference: str) -> bool:
        normalized = re.sub(r"[^a-z0-9]+", "", model_id.casefold())
        if preference == "k3":
            return "kimi" in normalized and "k3" in normalized
        return "opus5" in normalized or "claudeopus5" in normalized

    for preference in preferences:
        for model_ref, _ in available:
            if matches(model_ref.model_id, preference):
                label = "Kimi K3" if preference == "k3" else "Claude Opus 5"
                return model_ref, f"planner preference matched {label}"
    return manager, "preferred planner unavailable; fell back to manager model"


def select_capability_executor_model(
    catalog: dict[str, Any],
    preferred: ModelRef | None = None,
) -> tuple[ModelRef | None, str]:
    """Resolve the user's independent assistance slot without provider fallback."""
    if preferred is None:
        return None, "尚未配置辅助执行器；请在编排配置中选择 Provider、模型和推理强度。"
    for ref, details in _catalog_models(catalog):
        if (ref.provider_key, ref.model_id) != (preferred.provider_key, preferred.model_id):
            continue
        capabilities = details.get("capabilities") or []
        if (details.get("review_only") or details.get("tools") is False
                or details.get("protocol") == "typesafe"
                or (capabilities and "tools" not in capabilities)):
            return None, "该模型不支持辅助执行器所需的工具调用，请选择可执行工具的模型。"
        if preferred.effort and "efforts" in details and preferred.effort not in (details.get("efforts") or []):
            return None, "该辅助模型不支持所选推理强度，请重新选择。"
        return preferred, "configured capability executor"
    return None, (
        f"已配置的辅助执行器 {preferred.provider_key} / {preferred.model_id} 不可用；"
        "未自动替换提供方或模型。请重新选择可用模型。"
    )


@dataclass(frozen=True)
class ExpertExecutionPolicy:
    strategy: ExpertStrategy
    allow_delegation: bool
    max_direct_agents: int
    allow_recursive_delegation: bool = False
    max_iterations: int | None = None
    timeout_seconds: float | None = None
    soft_grace_seconds: float | None = None
    resume_messages: bool = True
    work_dir: str | None = None
    audit_contract: dict[str, Any] | None = None
    loop_context: dict[str, Any] | None = None
    token_budget: int | None = None
    usage_callback: Any = None
    handoff_holder: Any = None
    handoff_checks: tuple[dict[str, Any], ...] | None = None
    review_report_scope: dict[str, Any] | None = None
    stage_inputs: dict[str, Any] | None = None
    input_revision: str = ""
    node_epoch: int = 0
    required_review_snapshots: dict[str, str] | None = None
    node_tools: tuple[str, ...] | None = None
    deny_tools: tuple[str, ...] = ()
    parallel_readonly: bool = False
    parallel_isolated: bool = False
    collaboration_workspace: dict[str, Any] | None = None
    context_coordination: bool = False
    candidate_context: dict[str, str] | None = None
    command_allowlist: tuple[str, ...] = ()
    command_receipt_context: dict[str, Any] | None = None
    execution_recovery: bool = False
    expansion_holder: Any = None
    collaborative_expansion: bool = False
    recovery_decider: Any = None
    assistance_session: bool = False
    assistance_holder: Any = None
    recovery_analysis: Any = None
    authorization_reader: Any = None
    recovery_context_reader: Any = None
    authorization_mutator: Any = None
    execution_node_id: str = ''
    execution_scope_node_id: str = ''
    authorization_node_id: str = ''
    assistance_permission_log: Any = None
    assistance_protected_paths: tuple[str, ...] = ()
    investigation_snapshot: dict[str, Any] | None = None
    role_model_bindings: dict[str, Any] | None = None
    expansion_parent_contract: str = ""
    expansion_write_paths: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ExpertRoleContract:
    tools: tuple[str, ...]
    responsibility: str
    may_write: bool = False
    may_delegate: bool = False


EXPERT_ROLE_CONTRACTS: dict[ExpertRole, ExpertRoleContract] = {
    "manager": ExpertRoleContract(
        ("*",),
        "明确目标、约束、风险和可量化验收标准；按当前授权调查、验证和处理职责内问题，不自行委派。",
        may_write=True,
    ),
    "planner": ExpertRoleContract(
        ("*",),
        "根据已派生的成本策略制定实施方案；使用当前授权的工具取得实际证据、验证方案和修复前置条件，不自行委派。"
        "规划调查默认仅抽样观察代表页；扫描质量、答案位置和复杂图表分布等暂不能确认的内容，"
        "在调查记录中标为‘待试点核验’，交给授权门禁后的试点节点验证，不把试点验收前置为规划完成条件。"
        "保留全部文件与后续处理范围；明确要求本规划完成的专项核验仍按用户要求执行。",
        may_write=True,
    ),
    "executor": ExpertRoleContract(
        ("*",),
        "负责实施与验证；工具操作遵守当前授权，仅快速模式可创建直属成员。",
        may_write=True,
        may_delegate=True,
    ),
    "auditor": ExpertRoleContract(
        ("ReadFile", "Glob", "Grep", "InspectGitState", "ReadAuditArtifact", "InspectVisualEvidence", "ReadStageResult"),
        "只读审计现有变更并复用执行证据；不运行测试、不修改、不修复、不委派。",
    ),
}


# 所有专家角色输出的硬性报告规范：回复必须是提交给用户的正式报告，
# 不是工作日志。置于提示词最前，优先级高于其他所有要求。
REPORT_STYLE_CONTRACT = (
    "【输出格式】第一行给结论与一句话理由，正文简洁列出关键发现、证据和未完成项。"
    "用 report_step 说明必要的执行意图；具体业务或授权问题用 AskUserQuestion（关键抉择用 human_oncall）。"
    "按用户的业务对象汇报位置和问题，如书名、第5页/共110页、公式缺少一项、正在修正；"
    "数量用 report_step.progress，问题及处理用 issues，不能用循环轮数推算页数或拿内部异常代替情况说明。"
    "用 SubmitHandoff 提交结构化交接，正文面向用户；验收以独立评审为准。"
)


def derive_execution_policy(multi_agent: bool) -> ExpertExecutionPolicy:
    strategy: ExpertStrategy = "fast" if multi_agent else "stable"
    return ExpertExecutionPolicy(
        strategy=strategy,
        allow_delegation=multi_agent,
        max_direct_agents=8 if multi_agent else 0,
        allow_recursive_delegation=False,
    )


# executor 是专家团里唯一写文件、跑命令的角色，不能沿用主 Agent 的默认轮数；
# 复杂任务会撞上限。独立预算可通过环境变量覆盖。
_EXPERT_EXECUTOR_MAX_ITERATIONS_DEFAULT = 200
_EXPERT_RECOVERY_MAX_ITERATIONS_DEFAULT = 40
# A recovery pass is bounded by BOTH max_iterations and this wall clock, so the
# two must agree or the shorter one becomes a silent ceiling. At effort=max a
# turn costs ~60s, and 900s therefore cut every pass off after ~15 of the 40
# allowed turns: the executor spent whole segments working and was still judged
# to have made no progress. Keep this at or above iterations x per-turn cost.
_EXPERT_RECOVERY_TIMEOUT_SECONDS_DEFAULT = 60 * 60


def executor_max_iterations(base: int = 50) -> int:
    """返回 executor 角色的轮数预算，至少不低于 base，可被环境变量覆盖。"""
    raw = os.environ.get("MEWCODE_EXPERT_EXECUTOR_MAX_ITERATIONS")
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                return max(base, parsed)
        except ValueError:
            pass
    return max(base, _EXPERT_EXECUTOR_MAX_ITERATIONS_DEFAULT)


def recovery_executor_max_iterations() -> int:
    """Bound one focused audit-repair pass independently from main execution."""
    raw = os.environ.get("MEWCODE_EXPERT_RECOVERY_MAX_ITERATIONS")
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                return max(8, min(100, parsed))
        except ValueError:
            pass
    return _EXPERT_RECOVERY_MAX_ITERATIONS_DEFAULT


def recovery_executor_timeout_seconds() -> float:
    """Maximum wall time for one automatic repair pass.

    Must stay >= ``recovery_executor_max_iterations`` turns' worth of work, or
    the wall clock silently truncates a pass that still had budget left.
    """
    raw = os.environ.get("MEWCODE_EXPERT_RECOVERY_TIMEOUT_SECONDS")
    if raw:
        try:
            parsed = float(raw)
            if parsed > 0:
                return max(60.0, min(3600.0, parsed))
        except ValueError:
            pass
    return float(_EXPERT_RECOVERY_TIMEOUT_SECONDS_DEFAULT)


# 审计与规划的辩论轮数上限；达到上限仍未一致时由管理专家终裁。
MAX_DEBATE_ROUNDS = 2


def _verdict_passes(result: ExpertRoleResult) -> bool:
    """Structured verdict wins; legacy restored checkpoints pass through."""
    verdict = result.verdict or {}
    if verdict:
        return verdict.get("verdict") == "PASS"
    return result.restored and result.status == "completed"


# 定向返工的轮数上限与预算耗尽文案（checklist 钉死）。
REWORK_MAX_ROUNDS = 3
REWORK_BUDGET_ERROR = "定向返工已达上限，问题仍未解决，请人工介入。"
FINAL_RULING_ACCEPT = "FINAL_RULING: ACCEPT"
FINAL_RULING_REWORK = "FINAL_RULING: REWORK"


def parse_final_ruling(text: str) -> str:
    """终裁标记解析：accept / rework / unclear（无标记时按返工处理）。"""
    if FINAL_RULING_ACCEPT in text:
        return "accept"
    if FINAL_RULING_REWORK in text:
        return "rework"
    return "unclear"


RUN_DEADLINE_ERROR = "运行预算耗尽，请人工介入。"


@dataclass(frozen=True)
class ExpertRunBudget:
    """每一条沟通边的上限；耗尽即升级给人（needs_attention）。"""

    max_debate_rounds: int = MAX_DEBATE_ROUNDS
    max_rework_rounds: int = REWORK_MAX_ROUNDS
    deadline_seconds: float | None = None

    @classmethod
    def from_value(cls, value: Any) -> "ExpertRunBudget":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            return cls()
        return cls(
            max_debate_rounds=int(value.get("max_debate_rounds") or MAX_DEBATE_ROUNDS),
            max_rework_rounds=int(value.get("max_rework_rounds") or REWORK_MAX_ROUNDS),
            deadline_seconds=(
                float(value["deadline_seconds"])
                if value.get("deadline_seconds")
                else None
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_debate_rounds": self.max_debate_rounds,
            "max_rework_rounds": self.max_rework_rounds,
            "deadline_seconds": self.deadline_seconds,
        }


@dataclass
class ExpertRoleResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    status: str = "completed"
    topology: list[dict[str, Any]] = field(default_factory=list)
    wait_seconds: float = 0.0
    active_seconds: float = 0.0
    verdict: dict[str, Any] | None = None
    restored: bool = False
    verification_records: list[dict[str, Any]] = field(default_factory=list)
    command_receipts: list[dict[str, Any]] = field(default_factory=list)
    activity_records: list[dict[str, Any]] = field(default_factory=list)
    loop_decision: dict[str, Any] | None = None
    iterations_used: int = 0
    billable_tokens: int | None = None
    handoff: dict[str, Any] | None = None
    audit_snapshot: dict[str, Any] | None = None
    error_kind: str = ""
    failed_node_id: str = ""
    review_progress: dict[str, Any] | None = None
    work_progress: dict[str, Any] | None = None
    verification_request: dict[str, Any] | None = None
    node_expansion: dict[str, Any] | None = None
    node_expansion_rejection: dict[str, str] | None = None


@dataclass
class ExpertRunResult:
    run_id: str
    status: str
    strategy: str
    manager_summary: str
    planner_summary: str
    executor_summary: str
    audit_summary: str
    final_summary: str = ""
    node_summaries: dict[str, str] = field(default_factory=dict)
    transcript_path: str = ""


RoleRunner = Callable[
    [ExpertRole, ModelRef, str, ExpertExecutionPolicy],
    Awaitable[ExpertRoleResult],
]
NodeRunner = Callable[
    [ExpertNodeSpec, ModelRef, str, ExpertExecutionPolicy, ExpertMemberSpec | None],
    Awaitable[ExpertRoleResult],
]
EventSink = Callable[[dict[str, Any]], Awaitable[None] | None]
PipelineRunner = Callable[
    [ExpertNodeSpec, dict[str, Any], dict[str, Any] | None, EventSink],
    Awaitable[ExpertRoleResult],
]


class ExpertGroupOrchestrator:
    """Deterministic, checkpoint-aware four-stage expert flow."""

    def __init__(
        self,
        *,
        runner: RoleRunner,
        node_runner: NodeRunner | None = None,
        pipeline_runner: PipelineRunner | None = None,
        event_sink: EventSink | None = None,
        archive_root: str | os.PathLike[str] | None = None,
        cancel_callback: Callable[[], Awaitable[None] | None] | None = None,
        work_dir: str | os.PathLike[str] | None = None,
        lesson_store: Any = None,
        reflection_runner: Any = None,
        recovery_handler: Any = None,
        run_store: Any = None,
        command_admission: Any = None,
        execution_recovery_enabled: bool = False,
        collaboration_review: Any = None,
        collaboration_command: Any = None,
        collaboration_coordinate: Any = None,
        collaboration_publish: Any = None,
        collaboration_decide: Any = None,
        collaboration_context: Any = None,
    ) -> None:
        self.runner = runner
        self.node_runner = node_runner
        self.pipeline_runner = pipeline_runner
        self.event_sink = event_sink
        self.archive_root = Path(archive_root or user_data_root() / "expert-runs")
        self.cancel_callback = cancel_callback
        self.work_dir = Path(work_dir).resolve() if work_dir is not None else None
        self.lesson_store = lesson_store
        self.reflection_runner = reflection_runner
        self.recovery_handler = recovery_handler
        self.run_store = run_store
        self.command_admission = command_admission
        self.execution_recovery_enabled = execution_recovery_enabled
        self.collaboration_review = collaboration_review
        self.collaboration_command = collaboration_command
        self.collaboration_coordinate = collaboration_coordinate
        self.collaboration_publish = collaboration_publish
        self.collaboration_decide = collaboration_decide
        self.collaboration_context = collaboration_context

    async def _emit(self, event: dict[str, Any]) -> None:
        if self.event_sink is None:
            return
        result = self.event_sink(event)
        if inspect.isawaitable(result):
            await result

    async def _run_role(
        self,
        run_id: str,
        role: ExpertRole,
        model_ref: ModelRef,
        prompt: str,
        run_strategy: ExpertStrategy,
        policy: ExpertExecutionPolicy,
        fallback_reason: str,
        attempt: int,
    ) -> tuple[ExpertRoleResult, dict[str, Any]]:
        started_monotonic = time.monotonic()
        started_at = time.time()
        await self._emit({
            "type": "expert_stage", "run_id": run_id, "role": role,
            "provider": model_ref.provider_key, "model": model_ref.model_id,
            "effort": model_ref.effort, "status": "running",
            "fallback_reason": fallback_reason, "attempt": attempt,
            "strategy": run_strategy, "role_strategy": policy.strategy,
            "started_at": started_at,
        })
        result = await self.runner(
            role, model_ref, REPORT_STYLE_CONTRACT + "\n\n"
            + supervision_prompt(role) + "\n\n" + prompt, policy
        )
        record = {
            "role": role, "provider": model_ref.provider_key,
            "model": model_ref.model_id, "effort": model_ref.effort,
            "status": result.status, "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "duration_ms": round((time.monotonic() - started_monotonic) * 1000),
            "wait_seconds": round(result.wait_seconds, 3),
            "active_seconds": round(result.active_seconds, 3),
            "started_at": started_at, "completed_at": time.time(),
            "fallback_reason": fallback_reason, "topology": result.topology,
            "transcript": result.text, "attempt": attempt,
            "strategy": run_strategy, "role_strategy": policy.strategy,
        }
        await self._emit({
            "type": "expert_stage", "run_id": run_id,
            **{key: value for key, value in record.items() if key != "transcript"},
        })
        return result, record

    async def run(self, task, profile, catalog, *, strategy, flow=None, **kwargs):
        from mewcode.workflow_core.adapter import run_group
        from mewcode.workflow_core.schema import normalize
        if flow is None:
            flow = normalize(dict(goal=task, nodes=[dict(id='work', kind='work', label='执行任务', task=task)]))
        return await run_group(self, task=task, profile=profile, flow=flow, strategy=strategy, **kwargs)

    async def _run_legacy(
        self,
        task: str,
        profile: ExpertProfile | dict[str, Any],
        catalog: dict[str, Any],
        *,
        strategy: ExpertStrategy,
        run_id: str | None = None,
        attempt: int = 1,
        checkpoint: dict[str, Any] | None = None,
        supplemental: str = "",
        budget: ExpertRunBudget | None = None,
        flow: ExpertFlowSpec | dict[str, Any] | None = None,
        routers: dict[str, Any] | None = None,
        reducers: dict[str, Any] | None = None,
        finalizers: dict[str, Any] | None = None,
    ) -> ExpertRunResult:
        if flow is not None:
            from mewcode.kernel.collaboration import flow_mode
            parsed_flow = ExpertFlowSpec.from_value(flow)
            if flow_mode(parsed_flow.as_dict()) == "collaborative":
                from mewcode.orchestration.collab_runtime import CollaborationRuntime
                from mewcode.kernel.async_commit import durable_call
                collaboration = await durable_call(CollaborationRuntime, store=self.run_store, run_id=run_id, flow=parsed_flow,
                    catalog=catalog, work_dir=self.work_dir, review=self.collaboration_review, command=self.collaboration_command,
                    coordinate=self.collaboration_coordinate, publish=self.collaboration_publish,
                    decide=self.collaboration_decide, context=self.collaboration_context)
                return await collaboration.run(self, task=task, profile=profile, strategy=strategy,
                    attempt=attempt, budget=budget, supplemental=supplemental)
            return await self._run_graph(
                task,
                profile,
                catalog,
                strategy=strategy,
                run_id=run_id,
                attempt=attempt,
                checkpoint=checkpoint,
                supplemental=supplemental,
                budget=budget,
                flow=parsed_flow,
                routers=routers or {},
                reducers=reducers or {},
                finalizers=finalizers or {},
            )
        parsed_profile = ExpertProfile.from_value(profile)
        run_id = run_id or uuid.uuid4().hex
        records: list[dict[str, Any]] = []
        saved_stages = (checkpoint or {}).get("stages") or {}
        locked_models = (checkpoint or {}).get("locked_models") or {}
        no_delegation = derive_execution_policy(False)
        policy = derive_execution_policy(strategy == "fast")
        budget = ExpertRunBudget.from_value(budget)
        run_started = time.monotonic()
        saved_flow = (checkpoint or {}).get("flow_state") or {}
        budget_exhausted = False

        def within_budget() -> bool:
            nonlocal budget_exhausted
            if (
                budget.deadline_seconds is not None
                and (
                    budget.deadline_seconds <= 0
                    or time.monotonic() - run_started > budget.deadline_seconds
                )
            ):
                budget_exhausted = True
                return False
            return True

        def locked_ref(role: ExpertRole, fallback: ModelRef) -> ModelRef:
            value = locked_models.get(role)
            if not isinstance(value, dict):
                return fallback
            try:
                return ModelRef.from_value(value)
            except ValueError:
                return fallback

        def restored(role: ExpertRole) -> ExpertRoleResult | None:
            stage = saved_stages.get(role) or {}
            if stage.get("status") not in {"completed", "needs_attention"}:
                return None
            saved_verdict = stage.get("verdict")
            return ExpertRoleResult(
                text=str(stage.get("text") or stage.get("transcript") or ""),
                input_tokens=int(stage.get("input_tokens") or 0),
                output_tokens=int(stage.get("output_tokens") or 0),
                status=str(stage.get("status") or "completed"),
                topology=list(stage.get("topology") or []),
                verdict=saved_verdict if isinstance(saved_verdict, dict) else None,
                restored=True,
            )

        async def run_or_restore(
            role: ExpertRole,
            model_ref: ModelRef,
            prompt: str,
            role_policy: ExpertExecutionPolicy,
            fallback: str = "",
        ) -> ExpertRoleResult:
            prior = restored(role)
            if prior is not None:
                await self._emit({
                    "type": "expert_stage", "run_id": run_id, "role": role,
                    "status": "skipped", "reason": "checkpoint_completed",
                    "attempt": attempt, "strategy": strategy,
                })
                return prior
            result, record = await self._run_role(
                run_id, role, model_ref, prompt, strategy, role_policy,
                fallback, attempt,
            )
            records.append(record)
            return result

        try:
            manager = await run_or_restore(
                "manager", locked_ref("manager", parsed_profile.manager),
                EXPERT_ROLE_CONTRACTS["manager"].responsibility + "\n\nTASK:\n" + task,
                no_delegation,
            )
            kind = classify_task(task)
            if parsed_profile.planner is not None:
                planner_ref = parsed_profile.planner
                fallback = "explicit planner selection"
            else:
                planner_ref, fallback = select_planner_model(
                    parsed_profile.manager, catalog, kind,
                    parsed_profile.planner_routes.get(kind, ()),
                )
            planner_ref = locked_ref("planner", planner_ref)
            planner = await run_or_restore(
                "planner", planner_ref,
                EXPERT_ROLE_CONTRACTS["planner"].responsibility
                + f"\n本次策略已自动确定为 {strategy}；不要比较策略或等待用户选择。"
                + f"\n\nTASK:\n{task}\n\nMANAGER ANALYSIS:\n{manager.text}",
                no_delegation, fallback,
            )
            delegation = (
                "可使用 Agent 或 Team，直属成员共享上限 8，成员禁止递归委派；你负责整合。"
                if strategy == "fast"
                else "你是唯一执行者；Agent 和 Team 委派工具已禁用。"
            )
            resume_addendum = (
                "\n\n恢复补充要求：\n" + supplemental.strip()
                if supplemental.strip() else ""
            )
            manager_ref = locked_ref("manager", parsed_profile.manager)
            executor = await run_or_restore(
                "executor", locked_ref("executor", parsed_profile.executor),
                EXPERT_ROLE_CONTRACTS["executor"].responsibility
                + f"\n策略：{strategy}。{delegation}"
                + "\n结束时输出「完成度报告」：逐项列出每个子 agent / 交付物的"
                "完成状态（完整 / 缺失），并给出你对整体完成度的判断。"
                + f"\n\nTASK:\n{task}\n\nMANAGER:\n{manager.text}\n\nPLAN:\n{planner.text}"
                + resume_addendum,
                policy,
            )
            execution_truncated = executor.status == "budget_exhausted"
            audit_note = (
                "\n\n注意：执行阶段因达到轮数上限而提前结束，下面的 EXECUTION "
                "是已完成的部分成果，须按不完整对待并作为 NEEDS_ATTENTION 的证据。"
                if execution_truncated else ""
            )
            auditor = await run_or_restore(
                "auditor", locked_ref("auditor", parsed_profile.auditor),
                EXPERT_ROLE_CONTRACTS["auditor"].responsibility
                + "\n你负责第二套审批（合理性审计）；第一套完成度已由执行专家"
                "在「完成度报告」中产出，你只需核验。"
                "使用 RunVerification 运行白名单校验命令获取真实证据；"
                "审计结束必须调用 SubmitVerdict 工具提交结构化结论"
                "（PASS，或 NEEDS_ATTENTION + 带证据的问题清单）。"
                + f"\n\nTASK:\n{task}\n\nACCEPTANCE:\n{manager.text}"
                + f"\n\nEXECUTION:\n{executor.text}" + audit_note,
                no_delegation,
            )
            verdict = auditor.verdict or {}
            passed = _verdict_passes(auditor)
            flow = {
                "phase": str(saved_flow.get("phase") or ""),
                "debate_rounds": int(saved_flow.get("debate_rounds") or 0),
                "rework_rounds": int(saved_flow.get("rework_rounds") or 0),
                "ruling_text": str(saved_flow.get("ruling_text") or ""),
                "ruling_done": bool(saved_flow.get("ruling_done")),
                "last_verdict": saved_flow.get("last_verdict"),
                "passed": bool(saved_flow.get("passed")),
            }

            async def emit_flow() -> None:
                await self._emit({
                    "type": "expert_flow_state", "run_id": run_id,
                    "phase": flow["phase"],
                    "debate_rounds": flow["debate_rounds"],
                    "rework_rounds": flow["rework_rounds"],
                    "ruling_text": flow["ruling_text"],
                    "ruling_done": flow["ruling_done"],
                    "last_verdict": flow["last_verdict"],
                    "passed": flow["passed"],
                })

            flow["phase"] = "debate"
            flow["last_verdict"] = verdict
            flow["passed"] = passed
            await emit_flow()
            while (
                not passed
                and flow["debate_rounds"] < budget.max_debate_rounds
                and within_budget()
            ):
                flow["debate_rounds"] += 1
                await emit_flow()
                issues = verdict.get("issues") or []
                rebuttal, rebuttal_record = await self._run_role(
                    run_id, "planner", planner_ref,
                    EXPERT_ROLE_CONTRACTS["planner"].responsibility
                    + f"\n审计发现以下问题（辩论第 {flow['debate_rounds']} 轮）：\n{issues}"
                    + "\n请逐条回应：计划是否需要调整，或指出审计判断有误之处。"
                    + f"\n\nTASK:\n{task}\n\n原计划:\n{planner.text}",
                    strategy, no_delegation, "debate", attempt,
                )
                records.append(rebuttal_record)
                planner = rebuttal
                recheck, recheck_record = await self._run_role(
                    run_id, "auditor", locked_ref("auditor", parsed_profile.auditor),
                    EXPERT_ROLE_CONTRACTS["auditor"].responsibility
                    + f"\n规划专家对审计的回应（辩论第 {flow['debate_rounds']} 轮）：\n{planner.text}"
                    + "\n结合回应重新审计：问题已消除或证据不足时可判 PASS；"
                    "审计结束必须调用 SubmitVerdict 提交结构化结论。"
                    + f"\n\nTASK:\n{task}\n\nACCEPTANCE:\n{manager.text}"
                    + f"\n\nEXECUTION:\n{executor.text}",
                    strategy, no_delegation, "debate", attempt,
                )
                records.append(recheck_record)
                auditor = recheck
                verdict = auditor.verdict or {}
                passed = _verdict_passes(auditor)
                flow["last_verdict"] = verdict
                flow["passed"] = passed
                await emit_flow()
            if not passed and not flow["ruling_done"] and within_budget():
                ruling, ruling_record = await self._run_role(
                    run_id, "manager", manager_ref,
                    EXPERT_ROLE_CONTRACTS["manager"].responsibility
                    + "\n审计与规划辩论已达上限仍未一致，由你终裁。"
                    "在回复末尾以单独一行给出裁决："
                    "FINAL_RULING: ACCEPT（接受现状）或"
                    "FINAL_RULING: REWORK（必须返工）。"
                    + f"\n\nTASK:\n{task}\n\n审计结论:\n{verdict}"
                    + f"\n\n规划回应:\n{planner.text}",
                    strategy, no_delegation, "final_ruling", attempt,
                )
                records.append(ruling_record)
                flow["ruling_text"] = ruling.text
                flow["ruling_done"] = True
                flow["phase"] = "rework"
                await emit_flow()
                if parse_final_ruling(flow["ruling_text"]) == "accept":
                    passed = True
                    flow["passed"] = True
                    await emit_flow()
            if not passed:
                flow["phase"] = "rework"
            while (
                not passed
                and flow["rework_rounds"] < budget.max_rework_rounds
                and within_budget()
            ):
                flow["rework_rounds"] += 1
                await emit_flow()
                issues = verdict.get("issues") or []
                reworked, rework_record = await self._run_role(
                    run_id, "executor",
                    locked_ref("executor", parsed_profile.executor),
                    EXPERT_ROLE_CONTRACTS["executor"].responsibility
                    + f"\n策略：{strategy}。{delegation}"
                    + f"\n定向返工（第 {flow['rework_rounds']} 轮）：以下是审计定位的问题，"
                    "只修复这些问题，不扩大范围；之前的工作仍在工作区。"
                    + f"\n问题清单：\n{issues}"
                    + (
                        f"\n管理终裁意见：\n{flow['ruling_text']}"
                        if flow["ruling_text"] else ""
                    )
                    + f"\n\nTASK:\n{task}\n\nPLAN:\n{planner.text}",
                    strategy, policy, "rework", attempt,
                )
                records.append(rework_record)
                executor = reworked
                rework_truncated = reworked.status == "budget_exhausted"
                rework_note = (
                    "\n\n注意：定向返工因达到轮数上限而提前结束，下面的 EXECUTION "
                    "是不完整的部分成果，须按不完整对待并作为 NEEDS_ATTENTION 的证据。"
                    if rework_truncated else ""
                )
                recheck, recheck_record = await self._run_role(
                    run_id, "auditor",
                    locked_ref("auditor", parsed_profile.auditor),
                    EXPERT_ROLE_CONTRACTS["auditor"].responsibility
                    + f"\n定向返工复审（第 {flow['rework_rounds']} 轮）：只审本轮修复的变化集"
                    "与之前未通过的问题。审计结束必须调用 SubmitVerdict 提交结构化结论。"
                    + f"\n\nTASK:\n{task}\n\nACCEPTANCE:\n{manager.text}"
                    + f"\n\nEXECUTION:\n{executor.text}" + rework_note,
                    strategy, no_delegation, "rework", attempt,
                )
                records.append(recheck_record)
                auditor = recheck
                verdict = auditor.verdict or {}
                passed = _verdict_passes(auditor)
                flow["last_verdict"] = verdict
                flow["passed"] = passed
                await emit_flow()
            flow["phase"] = "done"
            flow["passed"] = passed
            flow["last_verdict"] = verdict
            await emit_flow()
            final_status = (
                "completed"
                if passed and executor.status != "budget_exhausted"
                else "needs_attention"
            )
            transcript_path = self._archive(
                run_id, task, strategy, final_status, records
            )
            audit_summary = auditor.text
            if flow["ruling_text"]:
                audit_summary += "\n\n[管理终裁]\n" + flow["ruling_text"]
            if final_status == "needs_attention":
                if budget_exhausted:
                    audit_summary += "\n\n" + RUN_DEADLINE_ERROR
                else:
                    audit_summary += "\n\n" + REWORK_BUDGET_ERROR
            result = ExpertRunResult(
                run_id=run_id, status=final_status, strategy=strategy,
                manager_summary=manager.text, planner_summary=planner.text,
                executor_summary=executor.text, audit_summary=audit_summary,
                transcript_path=transcript_path,
            )
            await self._emit({
                "type": "expert_run_done", "run_id": run_id,
                "status": final_status, "strategy": strategy,
                "attempt": attempt, "transcript_path": transcript_path,
            })
            return result
        except asyncio.CancelledError:
            await self._cancel_direct_agents()
            transcript_path = self._archive(run_id, task, strategy, "paused", records)
            await self._emit({
                "type": "expert_run_done", "run_id": run_id, "status": "paused",
                "strategy": strategy, "attempt": attempt,
                "transcript_path": transcript_path,
            })
            raise
        except Exception:
            await self._cancel_direct_agents()
            transcript_path = self._archive(run_id, task, strategy, "paused", records)
            await self._emit({
                "type": "expert_run_done", "run_id": run_id, "status": "paused",
                "strategy": strategy, "attempt": attempt,
                "transcript_path": transcript_path,
            })
            raise

    async def _run_graph(
        self,
        task: str,
        profile: ExpertProfile | dict[str, Any],
        catalog: dict[str, Any],
        *,
        strategy: ExpertStrategy,
        run_id: str | None,
        attempt: int,
        checkpoint: dict[str, Any] | None,
        supplemental: str,
        budget: ExpertRunBudget | None,
        flow: ExpertFlowSpec,
        routers: dict[str, Any],
        reducers: dict[str, Any],
        finalizers: dict[str, Any],
    ) -> ExpertRunResult:
        parsed_profile = ExpertProfile.from_value(profile)
        run_id = run_id or uuid.uuid4().hex
        checkpoint = copy.deepcopy(checkpoint or {})
        from mewcode.experts.branch_state import reconcile_unresolved_branch_skips
        reconcile_unresolved_branch_skips({**checkpoint, 'locked_flow': flow.as_dict(),
            'node_states': checkpoint.get('node_states') or checkpoint.get('stages') or {}})
        from mewcode.experts.command_receipts import reconcile_run_command_receipts
        reconcile_run_command_receipts(checkpoint)
        budget = ExpertRunBudget.from_value(budget)
        started = time.monotonic()
        records: list[dict[str, Any]] = []
        results: dict[str, ExpertRoleResult] = {}
        inactive_node_ids: set[str] = {identity for resolution in (checkpoint.get('fallback_resolutions') or {}).values()
                                      for identity in resolution.get('inactive_alternatives', [])}
        handoffs: dict[str, list[dict[str, Any]]] = {}
        injected_lessons: dict[str, list[dict[str, Any]]] = {}
        lesson_read_errors: list[str] = []
        node_attempts: dict[str, int] = {
            str(key): int(value or 0)
            for key, value in (checkpoint.get("node_attempts") or {}).items()
        }
        saved_states = checkpoint.get("node_states") or checkpoint.get("stages") or {}
        from mewcode.experts.attempt_memory import summarize_attempt, attempt_prompt, checkpoint_attempt_histories
        attempt_histories = checkpoint_attempt_histories(checkpoint)
        saved_members = checkpoint.get("members") or {}
        saved_pipeline_runs = checkpoint.get("pipeline_runs") or {}
        saved_expansions = checkpoint.get("node_expansions") or {}
        locked_models = checkpoint.get("locked_models") or {}
        flow.validate()
        from mewcode.orchestration.loops import compile_loops
        _, loop_contracts = compile_loops(flow.nodes, flow.edges, flow.meta.get("loops"))
        root_loops = {loop.entry_node_id: loop for loop in loop_contracts if not any(
            set(loop.node_ids) < set(parent.node_ids) for parent in loop_contracts
        )}
        loop_node_ids = {node_id for loop in loop_contracts for node_id in loop.node_ids}
        loop_reviewers = {node_id for loop in loop_contracts for node_id in loop.reviewer_node_ids}
        loop_states = dict(checkpoint.get("loop_states") or {})

        await self._emit({
            "type": "expert_flow_defined",
            "run_id": run_id,
            "flow": flow.as_dict(),
            "flow_id": flow.flow_id,
            "flow_version": flow.version,
            "attempt": attempt,
        })

        def over_deadline() -> bool:
            return (
                budget.deadline_seconds is not None
                and time.monotonic() - started > budget.deadline_seconds
            )

        def model_for(node: ExpertNodeSpec) -> tuple[ModelRef, str]:
            locked_value = (self.run_store.locked_model_for(run_id, node.node_id, node.role)
                            if self.run_store is not None and hasattr(self.run_store, "locked_model_for")
                            else None) or locked_models.get(node.node_id) or locked_models.get(node.role)
            if isinstance(locked_value, dict):
                try:
                    locked_ref = ModelRef.from_value(locked_value)
                    if (
                        not locked_ref.effort
                        and node.model is not None
                        and node.model.effort
                        and (locked_ref.provider_key, locked_ref.model_id)
                            == (node.model.provider_key, node.model.model_id)
                    ):
                        locked_ref = ModelRef(
                            provider_key=locked_ref.provider_key,
                            model_id=locked_ref.model_id,
                            effort=node.model.effort,
                        )
                    return locked_ref, "locked run model"
                except ValueError:
                    pass
            if node.model is not None:
                return node.model, "flow node model"
            if node.role == "executor":
                return parsed_profile.executor, "profile executor"
            if node.role == "auditor":
                return parsed_profile.auditor, "profile auditor"
            if node.role == "planner":
                if parsed_profile.planner is not None:
                    return parsed_profile.planner, "explicit planner selection"
                kind = classify_task(task)
                return select_planner_model(
                    parsed_profile.manager,
                    catalog,
                    kind,
                    parsed_profile.planner_routes.get(kind, ()),
                )
            return parsed_profile.manager, "profile manager"

        constraint_pipelines = (
            (flow.meta or {}).get("constraint_pipelines")
            if isinstance((flow.meta or {}).get("constraint_pipelines"), dict)
            else {}
        )
        skipped_schedule_ids = {
            str(item).strip()
            for item in ((flow.meta or {}).get("schedule_skipped_node_ids") or [])
            if str(item).strip()
        }

        def policy_for(node: ExpertNodeSpec, *, member: bool = False) -> ExpertExecutionPolicy:
            pipeline = constraint_pipelines.get(node.node_id)
            child_count = len(pipeline) if isinstance(pipeline, list) else 0
            base = derive_execution_policy(not member and strategy == "fast" and node.role == "executor")
            if not member and child_count > 0 and node.role == "executor":
                base = replace(base, allow_delegation=True, max_direct_agents=max(1, min(8, child_count)))
            def domain_decide(*, trigger, made_progress=False, identity):
                from mewcode.kernel.escalation import decide_and_record
                from mewcode.experts.recovery_state import digest
                from dataclasses import asdict
                selected = None
                def choose(record):
                    nonlocal selected
                    stage = record.setdefault("node_states", {}).setdefault(node.node_id, {})
                    selected = decide_and_record(record, node.node_id, trigger=trigger,
                        made_progress=made_progress, identity=digest(["domain", trigger, identity]))
                    if selected.action not in {"progress_continue", "self_diagnose"}:
                        stage["pending_recovery_decision"] = {"decision": asdict(selected), "epoch": stage.get("epoch", 0)}
                if self.run_store is not None:
                    self.run_store.mutate(run_id, choose, durable=True)
                else:
                    choose(checkpoint)
                return selected
            live = (self.run_store.load(run_id) if self.run_store is not None else checkpoint) or {}
            epoch = ((live.get('node_states') or {}).get(node.node_id) or {}).get('epoch', 0)
            return replace(base, recovery_decider=domain_decide, node_epoch=epoch)

        def is_success(node: ExpertNodeSpec, result: ExpertRoleResult) -> bool:
            if result.status != "completed":
                return False
            if (
                node.kind == "role"
                and node.role == "auditor"
            ):
                return _verdict_passes(result)
            return True

        async def reduce_members(
            node: ExpertNodeSpec,
            member_results: list[tuple[ExpertMemberSpec, ExpertRoleResult]],
        ) -> str:
            reducer = reducers.get(node.reducer)
            if callable(reducer):
                value = reducer(node, member_results)
                if inspect.isawaitable(value):
                    value = await value
                return str(value or "")
            return "\n\n".join(
                f"[{member.name}]\n{result.text}"
                for member, result in member_results
            )

        def base_prompt(
            node: ExpertNodeSpec,
            incoming: list[dict[str, Any]],
            member_summary: str = "",
        ) -> str:
            contract = EXPERT_ROLE_CONTRACTS.get(node.role)
            responsibility = node.responsibility or (
                contract.responsibility if contract is not None else "完成分配给你的专家职责。"
            )
            context_text = "\n\n".join(
                f"FROM {item['from_node']} ({item.get('label') or ''}):\n"
                + str((item.get("handoff") or {}).get("summary") or item.get("text") or "")[:1200]
                for item in incoming
            )
            parts = [responsibility, f"NODE: {node.node_id}", f"TASK:\n{task}"]
            parts.append(supervision_prompt(node.role))
            if self.run_store is not None and self.work_dir is not None:
                from mewcode.experts.node_knowledge_cards import select_cards
                from mewcode.experts.stage_context import collect_stage_inputs, input_revision
                try:
                    current_stage = ((self.run_store.load(run_id) or {}).get("node_states") or {}).get(node.node_id) or {}
                    current_revision = input_revision(sources=collect_stage_inputs(incoming, handoffs, results),
                        flow=flow, node=node, model=model_for(node)[0])
                    cards = select_cards(current_stage, input_revision=current_revision, root=self.work_dir)
                except (OSError, ValueError):
                    cards = []
                if cards:
                    parts.append("节点知识卡（宿主工具验证，输入或文件变更即失效；不改变验收或权限）：\n"
                        + "\n".join("- " + card["text"] for card in cards))
            parts.append("共享执行规则：先核对已知输入与上游证据；只完成本节点的增量操作。"
                         "不编造事实、不扩大用户授权范围；缺失关键输入或需改变目标时交接用户。")
            parts.append('取证工具选择：文本用 ReadFile 或 ReadStageResult；图像和 PDF 版式用 '
                         'ReadAuditArtifact(render_page=true)；命令用 RunVerification；模型不支持图片时用 '
                         'InspectVisualEvidence 取得观察。文字提取不能当作像素证据，观察不能作为授权或验收；'
                         '判定仍由当前审阅者给出。所需工具不可用时报告能力缺失并保留 UNKNOWN。')
            if flow.meta.get("shared_constraints"):
                parts.append("图级共享约束：\n" + json.dumps(flow.meta["shared_constraints"], ensure_ascii=False))
            if self.lesson_store is not None:
                if node.node_id not in injected_lessons:
                    try:
                        injected_lessons[node.node_id] = self.lesson_store.select(flow.flow_id, node.node_id, node.role)
                    except (OSError, ValueError) as exc:
                        injected_lessons[node.node_id] = []
                        lesson_read_errors.append(str(exc)[:300])
                if injected_lessons[node.node_id]:
                    parts.append("经验提示（来自此前运行，按需采用；与 TASK、授权或冻结验收冲突时忽略）：\n"
                                 + "\n".join("- " + item["text"] for item in injected_lessons[node.node_id]))
            if supplemental.strip():
                parts.append("SUPPLEMENTAL REQUIREMENTS:\n" + supplemental.strip())
            if context_text:
                parts.append("UPSTREAM HANDOFFS:\n" + context_text)
            from mewcode.experts.stage_context import collect_stage_inputs, stage_inputs_prompt
            parts.append(stage_inputs_prompt(collect_stage_inputs(incoming, handoffs, results)))
            if node.node_id == flow.finalizer_node_id:
                from mewcode.tools.submit_handoff import fallback_handoff
                stage_handoffs = []
                for batch in flow.topological_batches():
                    for node_id in batch:
                        if node_id == node.node_id or node_id not in results:
                            continue
                        result = results[node_id]
                        handoff = result.handoff or fallback_handoff(result.text, verdict=result.verdict)
                        stage_handoffs.append({"node_id": node_id, "status": result.status,
                            "summary": str(handoff.get("summary", ""))[:600],
                            "open_items": [str(item)[:300] for item in (handoff.get("open_items") or [])[:8]]})
                parts.append("ALL STAGE HANDOFFS:\n" + json.dumps(stage_handoffs, ensure_ascii=False))
            if member_summary:
                parts.append("MEMBER RESULTS:\n" + member_summary)
            if node.role in {"executor", "planner", "manager"}:
                parts.append("结束前必须调用 SubmitHandoff，status 表示任务实际完成情况。"
                    "已执行并核对产物才填 completed；未实施、缺输入或权限时填 blocked/needs_attention 并列 blockers。"
                    "列出实际产物与验证证据；仅写一份受阻交接文件不算完成原任务。")
                has_constraint_pipeline = isinstance(
                    constraint_pipelines.get(node.node_id), list
                ) and bool(constraint_pipelines.get(node.node_id))
                if has_constraint_pipeline:
                    parts.append(
                        "本阶段含约束管线：默认由你一次完成；仅当某条约束需要独立深入时，"
                        "再按需创建直属子 agent；成员仅执行分配任务；你负责整合所有结果。"
                    )
                elif strategy == "fast":
                    parts.append(
                        "可委派直属成员，但成员仅执行分配任务；你负责整合所有结果。"
                    )
                else:
                    parts.append("你是唯一执行者，额外能力通过 RequestAssistance 获取。")
            if node.role == "auditor" and node.node_id not in loop_reviewers:
                parts.append(
                    "必须调用 SubmitVerdict 提交 PASS 或 NEEDS_ATTENTION 结构化结论。"
                    "NEEDS_ATTENTION 的每一项必须给稳定 issue_id；复审时未解决项"
                    "沿用原 issue_id，已解决项删除，相同问题保持原有身份。"
                    "终校 PASS 必须核验上游冻结审查的全部问题，并在 reviewed_snapshots 中引用其 node_id -> snapshot_hash。"
                    "仅核验无关内容或未关闭上游问题，应提交 NEEDS_ATTENTION。"
                )
            if node.node_id == flow.finalizer_node_id:
                parts.append(
                    "你是最终汇总节点。综合所有上游结论，直接给出面向用户的最终答复；"
                    "正文聚焦交付内容与验收证据。"
                )
            from mewcode.orchestration.run_inputs import append_resolved_inputs
            latest_inputs = (self.run_store.load(run_id) if self.run_store is not None else None) or checkpoint
            brief = (flow.meta.get("node_plan_briefs") or {}).get(node.node_id) or {}
            return append_resolved_inputs("\n\n".join(parts),
                declarations=flow.meta.get("run_input_contract") or [], values=latest_inputs.get("run_inputs") or {},
                known_inputs=brief.get("known_inputs") or [],
                upstream_sources=collect_stage_inputs(incoming, handoffs, results))

        async def call_runner(
            node: ExpertNodeSpec,
            model_ref: ModelRef,
            prompt: str,
            policy: ExpertExecutionPolicy,
            member: ExpertMemberSpec | None = None,
        ) -> ExpertRoleResult:
            if self.node_runner is not None:
                return await self.node_runner(node, model_ref, prompt, policy, member)
            return await self.runner(node.role, model_ref, prompt, policy)

        async def run_member(
            node: ExpertNodeSpec,
            member: ExpertMemberSpec,
            model_ref: ModelRef,
            incoming: list[dict[str, Any]],
        ) -> tuple[ExpertMemberSpec, ExpertRoleResult]:
            saved = saved_members.get(f"{node.node_id}::{member.member_id}") or {}
            if saved.get("status") == "completed":
                result = ExpertRoleResult(
                    text=str(saved.get("text") or saved.get("partial_text") or ""),
                    input_tokens=int(saved.get("input_tokens") or 0),
                    output_tokens=int(saved.get("output_tokens") or 0),
                    status="completed",
                    restored=True,
                )
                await self._emit({
                    "type": "expert_member_completed",
                    "run_id": run_id,
                    "node_id": node.node_id,
                    "role": node.role,
                    "member_id": member.member_id,
                    "member_name": member.name,
                    "status": "completed",
                    "restored": True,
                })
                return member, result
            member_model = member.model or model_ref
            member_started = time.time()
            await self._emit({
                "type": "expert_member_started",
                "run_id": run_id,
                "node_id": node.node_id,
                "role": node.role,
                "member_id": member.member_id,
                "member_name": member.name,
                "started_at": member_started,
                "provider": member_model.provider_key,
                "model": member_model.model_id,
            })
            prompt = base_prompt(node, incoming) + "\n\nMEMBER TASK:\n" + member.task
            from mewcode.experts.stage_context import collect_stage_inputs, input_revision, runtime_prompt
            sources = collect_stage_inputs(incoming, handoffs, results)
            member_policy = replace(policy_for(node, member=True), stage_inputs=sources,
                input_revision=input_revision(sources=sources, flow=flow, node=node, model=member_model),
                node_tools=node.tools, deny_tools=node.deny_tools)
            prompt += runtime_prompt(run_id=run_id, flow=flow, node_id=node.node_id,
                model=member_model, work_dir=self.work_dir, attempt=attempt)
            result = await call_runner(
                node, member_model, REPORT_STYLE_CONTRACT + "\n\n" + prompt,
                member_policy, member,
            )
            await self._emit({
                "type": "expert_member_completed",
                "run_id": run_id,
                "node_id": node.node_id,
                "role": node.role,
                "member_id": member.member_id,
                "member_name": member.name,
                "status": result.status,
                "started_at": member_started,
                "completed_at": time.time(),
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            })
            return member, result

        async def execute_node(
            node: ExpertNodeSpec,
            incoming: list[dict[str, Any]],
            *,
            recovery_round: int = 0,
            policy_override: ExpertExecutionPolicy | None = None,
            extra_prompt: str = "",
        ) -> ExpertRoleResult:
            nonlocal flow
            from mewcode.experts.continuation import NodeContinuation
            # Productive segments are ordinary scheduling, not recursive retries.
            # Keep only the current suggestion; a long run must not accumulate
            # Python frames or re-inject its complete Auto decision history.
            fault_attempts, work_cursor = 0, None
            base_prompt = extra_prompt
            while True:
                # An accepted assistance patch is scoped to this live run. Read
                # its node definition before the callback and downstream work.
                if self.run_store is not None:
                    live = self.run_store.load(run_id) or {}
                    if live.get("assistance_flow_revision"):
                        flow = ExpertFlowSpec.from_value(live["locked_flow"])
                        node = flow.node_map.get(node.node_id, node)
                        if policy_override is not None:
                            policy_override = replace(policy_override, node_tools=node.tools,
                                                      deny_tools=node.deny_tools,
                                                      node_epoch=((live.get('node_states') or {}).get(node.node_id) or {}).get('epoch', 0))
                from mewcode.experts.waits import BranchWaitPaused
                try:
                    outcome = await execute_node_segment(node, incoming,
                        recovery_round=recovery_round, policy_override=policy_override,
                        extra_prompt=extra_prompt, scheduler_depth=fault_attempts,
                        work_cursor=work_cursor, continuation_base_prompt=base_prompt)
                except BranchWaitPaused as exc:
                    return ExpertRoleResult(text=exc.reason or "该分支等待用户决定，其他独立分支继续。", status="paused",
                                            error_kind=exc.error_kind, failed_node_id=exc.node_id)
                if not isinstance(outcome, NodeContinuation):
                    return outcome
                policy_override, extra_prompt = outcome.policy, outcome.prompt
                work_cursor = outcome.cursor
                fault_attempts = 0 if outcome.productive else fault_attempts + 1

        async def execute_node_segment(
            node: ExpertNodeSpec,
            incoming: list[dict[str, Any]],
            *,
            recovery_round: int = 0,
            policy_override: ExpertExecutionPolicy | None = None,
            extra_prompt: str = "",
            scheduler_depth: int = 0,
            work_cursor: dict | None = None,
            continuation_base_prompt: str = "",
        ):
            if over_deadline():
                return ExpertRoleResult(text=RUN_DEADLINE_ERROR, status="budget_exhausted", error_kind="run_deadline")
            saved = saved_states.get(node.node_id) or {}
            saved_expansion = saved_expansions.get(node.node_id) or {}
            expansion_stale_reason = ""
            if (
                isinstance(saved_expansion, dict)
                and saved_expansion.get("proposal")
                and saved_expansion.get("status")
                in {"accepted", "running", "joining", "blocked", "completed"}
            ):
                saved_proposal = saved_expansion.get("proposal") or {}
                if int(saved_proposal.get("expected_flow_version") or 0) != flow.version:
                    expansion_stale_reason = "flow_version_changed"
                elif (
                    int(saved_expansion.get("node_attempt") or 0)
                    != int(node_attempts.get(node.node_id) or 0)
                ):
                    expansion_stale_reason = "node_attempt_changed"
                elif node.kind == "role":
                    from mewcode.experts.stage_context import (
                        collect_stage_inputs as collect_expansion_inputs,
                        input_revision as expansion_input_revision,
                    )
                    current_model, _ = model_for(node)
                    current_sources = collect_expansion_inputs(
                        incoming, handoffs, results
                    )
                    current_revision = expansion_input_revision(
                        sources=current_sources,
                        flow=flow,
                        node=node,
                        model=current_model,
                    )
                    if (
                        not saved_expansion.get("input_revision")
                        or saved_expansion.get("input_revision") != current_revision
                    ):
                        expansion_stale_reason = "input_revision_changed"
                if expansion_stale_reason:
                    saved_expansion["status"] = "invalidated"
                    await self._emit({
                        "type": "expert_node_expansion_invalidated",
                        "run_id": run_id,
                        "node_id": node.node_id,
                        "role": node.role,
                        "expansion_id": saved_expansion.get("expansion_id"),
                        "status": "invalidated",
                        "reason": expansion_stale_reason,
                    })
            if (
                policy_override is None
                and recovery_round == 0
                and isinstance(saved_expansion, dict)
                and saved_expansion.get("proposal")
                and not expansion_stale_reason
                and (
                    saved_expansion.get("status")
                    in {"accepted", "running", "joining", "blocked"}
                    or (
                        saved_expansion.get("status") == "completed"
                        and str(saved.get("status") or "") != "completed"
                    )
                )
            ):
                # The parent request already stopped after submitting this
                # immutable proposal.  Resume the overlay, never replay the
                # parent conversation to obtain the same proposal again.
                return ExpertRoleResult(
                    text=str(saved.get("text") or "父节点已提交运行时微图。"),
                    input_tokens=int(saved.get("input_tokens") or 0),
                    output_tokens=int(saved.get("output_tokens") or 0),
                    billable_tokens=(
                        int(saved.get("billable_tokens"))
                        if saved.get("billable_tokens") is not None
                        else None
                    ),
                    status="expanding",
                    handoff=saved.get("handoff"),
                    restored=True,
                    node_expansion=copy.deepcopy(saved_expansion),
                )
            # Only successful completions are skipped on resume. HITL /
            # needs_attention / failed stages must re-run so the graph can
            # advance after the user unblocks them.
            if policy_override is None and recovery_round == 0 and str(saved.get("status") or "") == "completed":
                saved_verdict = saved.get("verdict")
                restored = ExpertRoleResult(
                    text=str(
                        saved.get("text")
                        or saved.get("partial_text")
                        or saved.get("transcript")
                        or ""
                    ),
                    input_tokens=int(saved.get("input_tokens") or 0),
                    output_tokens=int(saved.get("output_tokens") or 0),
                    status="completed",
                    topology=list(saved.get("topology") or []),
                    verdict=saved_verdict if isinstance(saved_verdict, dict) else None,
                    handoff=saved.get("handoff"),
                    audit_snapshot=saved.get("audit_snapshot"),
                    restored=True,
                )
                if node.role == "auditor" and restored.verdict and not restored.audit_snapshot:
                    from mewcode.experts.stage_context import review_snapshot
                    restored.audit_snapshot = review_snapshot(run_id=run_id, flow_version=flow.version,
                        node_id=node.node_id, verdict=restored.verdict)
                auditor_incomplete = (
                    node.kind == "role"
                    and node.role == "auditor"
                    and (restored.verdict or {}).get("verdict") not in {"PASS", "NEEDS_ATTENTION"}
                )
                from mewcode.experts.gate_dialogue import confirmation_valid
                gate_confirmation = (checkpoint.get("gate_confirmations") or {}).get(node.node_id) or {}
                if not isinstance(gate_confirmation, dict):
                    gate_confirmation = {}
                gate_unconfirmed = ((flow.meta.get("node_kinds") or {}).get(node.node_id) == "gate"
                    and not confirmation_valid(node, flow, checkpoint))
                if not auditor_incomplete and not gate_unconfirmed:
                    await self._emit({
                        "type": "expert_node_completed",
                        "run_id": run_id,
                        "node_id": node.node_id,
                        "role": node.role,
                        "label": node.label,
                        "status": restored.status,
                        "passed": is_success(node, restored),
                        "audit_snapshot": restored.audit_snapshot,
                        "restored": True,
                        "node_attempt": node_attempts.get(node.node_id, 1),
                    })
                    await self._emit({
                        "type": "expert_stage",
                        "run_id": run_id,
                        "node_id": node.node_id,
                        "role": node.role,
                        "label": node.label,
                        "status": "skipped",
                        "reason": "checkpoint_completed",
                        "attempt": attempt,
                    })
                    return restored
            model_ref: ModelRef | None = None
            if node.kind == "pipeline":
                pipeline_ref = node.pipeline
                if pipeline_ref is None:
                    return ExpertRoleResult(
                        text=f"pipeline node {node.node_id} has no pipeline reference",
                        status="failed",
                    )
                provider_name = "pipeline"
                model_name = pipeline_ref.graph_id
                fallback = "embedded project pipeline"
            else:
                model_ref, fallback = model_for(node)
                provider_name = model_ref.provider_key
                model_name = model_ref.model_id
            member_summary = ""
            if node.members:
                await self._emit({
                    "type": "expert_join_started",
                    "run_id": run_id,
                    "node_id": node.node_id,
                    "role": node.role,
                    "member_count": len(node.members),
                })
                member_results = await asyncio.gather(*(
                    run_member(node, member, model_ref, incoming)
                    for member in node.members
                ))
                member_summary = await reduce_members(node, list(member_results))
                await self._emit({
                    "type": "expert_join_completed",
                    "run_id": run_id,
                    "node_id": node.node_id,
                    "role": node.role,
                    "member_count": len(node.members),
                    "reducer": node.reducer,
                })

            # The outer audit recovery loop already owns retries.  Applying a
            # repair node's ordinary retry policy as well would multiply the
            # budget (audit rounds × node retries × agent iterations).
            max_attempts = 1 if recovery_round > 0 or policy_override is not None else (
                node.failure_policy.max_attempts
                if node.failure_policy.action == "retry"
                else 1
            )
            result = ExpertRoleResult(text="", status="failed")
            audit_protocol_retry = False
            capability_followup_used = False
            # One protocol-only repair is separate from executor repair. A
            # second missing verdict stops, even when the node has retry=3.
            audit_protocol_slot = node.role == "auditor" and not (policy_override and policy_override.loop_context)
            for retry_index in range(max_attempts + int(audit_protocol_slot) + int(node.role in {"manager", "planner", "executor"})):
                required_reviews = {}
                node_attempts[node.node_id] = node_attempts.get(node.node_id, 0) + 1
                started_at = time.time()
                await self._emit({
                    "type": "expert_node_started",
                    "run_id": run_id,
                    "node_id": node.node_id,
                    "role": node.role,
                    "label": node.label,
                    "status": "running",
                    "started_at": started_at,
                    "node_attempt": node_attempts[node.node_id],
                    "recovery_round": recovery_round,
                    "provider": provider_name,
                    "model": model_name,
                    "effort": model_ref.effort if model_ref is not None else "",
                })
                await self._emit({
                    "type": "expert_stage",
                    "run_id": run_id,
                    "node_id": node.node_id,
                    "role": node.role,
                    "label": node.label,
                    "status": "running",
                    "started_at": started_at,
                    "provider": provider_name,
                    "model": model_name,
                    "effort": model_ref.effort if model_ref is not None else "",
                    "attempt": attempt,
                })
                try:
                    if node.kind == "pipeline":
                        if self.pipeline_runner is None:
                            raise RuntimeError(
                                f"pipeline runtime is unavailable for node {node.node_id}"
                            )

                        async def relay_pipeline_event(
                            pipeline_event: dict[str, Any],
                        ) -> None:
                            payload = dict(pipeline_event)
                            payload.setdefault("run_id", run_id)
                            payload.setdefault("node_id", node.node_id)
                            payload.setdefault("role", "pipeline")
                            payload.setdefault("pipeline_id", pipeline_ref.graph_id)
                            await self._emit(payload)

                        trigger_input = {
                            "task": task,
                            "supplemental": supplemental.strip(),
                            "instruction": node.responsibility,
                            "handoffs": [dict(item) for item in incoming],
                            "expert": {
                                "run_id": run_id,
                                "flow_id": flow.flow_id,
                                "flow_version": flow.version,
                                "node_id": node.node_id,
                            },
                        }
                        await self._emit({
                            "type": "expert_pipeline_started",
                            "run_id": run_id,
                            "node_id": node.node_id,
                            "role": "pipeline",
                            "label": node.label,
                            "pipeline_id": pipeline_ref.graph_id,
                            "entry_node_id": pipeline_ref.entry_node_id,
                            "started_at": started_at,
                        })
                        try:
                            result = await self.pipeline_runner(
                                node,
                                trigger_input,
                                saved_pipeline_runs.get(node.node_id),
                                relay_pipeline_event,
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            await self._emit({
                                "type": "expert_pipeline_completed",
                                "run_id": run_id,
                                "node_id": node.node_id,
                                "role": "pipeline",
                                "label": node.label,
                                "pipeline_id": pipeline_ref.graph_id,
                                "status": "failed",
                                "error": str(exc),
                                "completed_at": time.time(),
                                "duration_ms": round((time.time() - started_at) * 1000),
                            })
                            raise
                        else:
                            await self._emit({
                                "type": "expert_pipeline_completed",
                                "run_id": run_id,
                                "node_id": node.node_id,
                                "role": "pipeline",
                                "label": node.label,
                                "pipeline_id": pipeline_ref.graph_id,
                                "status": result.status,
                                "completed_at": time.time(),
                                "duration_ms": round((time.time() - started_at) * 1000),
                            })
                    else:
                        assert model_ref is not None
                        execution_policy = policy_override or policy_for(node)
                        execution_policy = replace(execution_policy, node_tools=node.tools, deny_tools=node.deny_tools)
                        from mewcode.experts.stage_context import collect_stage_inputs, runtime_prompt, input_revision
                        stage_inputs = collect_stage_inputs(incoming, handoffs, results)
                        from mewcode.experts.outcome import downstream_reviews
                        required_reviews = {key: source["audit_snapshot"]["snapshot_hash"]
                            for key, source in stage_inputs.items() if source.get("audit_snapshot")
                            and (source.get("verdict") or {}).get("verdict") == "NEEDS_ATTENTION"
                            and node.node_id in downstream_reviews(flow.as_dict(), key)} if node.role == "auditor" else {}
                        revision = input_revision(
                            # Host results enrich this attempt; they do not change
                            # the business inputs the requests were bound to.
                            sources={key: source for key, source in stage_inputs.items()
                                     if key not in {item.get("from_node") for item in incoming
                                                    if item.get("condition") == "capability_followup"}},
                            flow=flow, node=node, model=model_ref
                        )
                        execution_policy = replace(execution_policy,
                            stage_inputs=stage_inputs, input_revision=revision,
                            required_review_snapshots=required_reviews)
                        expansion_eligible = (
                            policy_override is None
                            and recovery_round == 0
                            and node.kind == "role"
                            and node.role == "executor"
                            and node.node_id != flow.finalizer_node_id
                            and node.node_id not in loop_node_ids
                            and not node.members
                        )
                        if expansion_eligible:
                            holder = execution_policy.expansion_holder
                            if holder is None:
                                from mewcode.experts.node_expansion import (
                                    NodeExpansionContext,
                                    NodeExpansionHolder,
                                )
                                from mewcode.permissions.policy import build_permission_checker
                                expansion_work_dir = (
                                    self.work_dir or Path.cwd()
                                ).resolve()
                                holder = NodeExpansionHolder(NodeExpansionContext(
                                    parent_node_id=node.node_id,
                                    flow_version=flow.version,
                                    input_revision=revision,
                                    node_attempt=node_attempts[node.node_id],
                                    work_dir=expansion_work_dir,
                                    allowed_tools=frozenset(),
                                    available_input_refs=frozenset(stage_inputs),
                                    sandbox=build_permission_checker(
                                        expansion_work_dir,
                                        writable_scope=[expansion_work_dir],
                                    ).sandbox,
                                ))
                            else:
                                # The web runtime may provide the holder early;
                                # stamp it only after the exact attempt inputs
                                # are frozen so stale proposals cannot activate.
                                holder.context.parent_node_id = node.node_id
                                holder.context.flow_version = flow.version
                                holder.context.input_revision = revision
                                holder.context.node_attempt = node_attempts[node.node_id]
                                holder.context.available_input_refs = frozenset(stage_inputs)
                            execution_policy = replace(
                                execution_policy, expansion_holder=holder
                            )
                        else:
                            execution_policy = replace(
                                execution_policy, expansion_holder=None
                            )
                        execution_prompt = REPORT_STYLE_CONTRACT + "\n\n" + base_prompt(
                            node, incoming, member_summary
                        )
                        if extra_prompt:
                            execution_prompt += "\n\n" + extra_prompt
                        execution_prompt += attempt_prompt(attempt_histories.get(node.node_id) or [],
                                                           attempt=node_attempts[node.node_id])
                        execution_prompt += runtime_prompt(run_id=run_id, flow=flow, node_id=node.node_id,
                            model=model_ref, work_dir=execution_policy.work_dir or self.work_dir,
                            attempt=node_attempts[node.node_id])
                        if audit_protocol_retry:
                            execution_policy = replace(execution_policy,
                                max_iterations=min(execution_policy.max_iterations or 4, 4),
                                timeout_seconds=min(execution_policy.timeout_seconds or 120, 120))
                            execution_prompt += ("\n\n【审计协议补交 · 仅此一次】上次未提交有效的结构化结论。"
                                "保持本轮验收范围、已采集证据和所有冻结哈希不变；你必须调用 SubmitVerdict"
                                "（量化审计必须附 assessment），正文不能代替工具提交。"
                                "证据不足应明确 UNKNOWN/未通过，不要编造 PASS，不要让 executor 修复一个没有结论的审计。")
                        if recovery_round > 0 and node.role == "executor":
                            execution_policy = replace(
                                execution_policy,
                                max_iterations=recovery_executor_max_iterations(),
                                timeout_seconds=recovery_executor_timeout_seconds(),
                                resume_messages=False,
                            )
                            execution_prompt += (
                                "\n\n【定向修复收敛约束】\n"
                                f"这是第 {recovery_round} 轮定向修复。只处理上游列出的未通过项，"
                                "不得扩大到润色或额外重构。每项最多采用两种修复路径；完成一次"
                                "针对性验证后立即结束并报告：已改文件、验证证据、仍未解决项。"
                                "若无法产生新证据，直接报告阻塞，不得重复尝试同类工具。"
                                "上游方案与你上次交接见 UPSTREAM HANDOFFS，不要重新调查已确认事实。"
                            )
                        runner_call = call_runner(
                            node,
                            model_ref,
                            execution_prompt,
                            execution_policy,
                        )
                        if execution_policy.timeout_seconds is not None:
                            try:
                                result = await asyncio.wait_for(
                                    runner_call,
                                    timeout=execution_policy.timeout_seconds,
                                )
                            except TimeoutError:
                                result = ExpertRoleResult(
                                    text=(
                                        "节点执行段超过时限 "
                                        f"{int(execution_policy.timeout_seconds)} 秒，"
                                        "已保留检查点，等待调度恢复决策。"
                                    ),
                                    status="budget_exhausted",
                                    error_kind="segment_timeout",
                                )
                                checkpoint_result = getattr(self.recovery_handler, "checkpoint_result", None)
                                if callable(checkpoint_result):
                                    result = checkpoint_result(node.node_id, result)
                        else:
                            result = await runner_call
                except asyncio.CancelledError:
                    raise
                except BranchWaitPaused:
                    raise
                except Exception as exc:
                    result = ExpertRoleResult(text=format_error(exc), status="failed")
                if audit_protocol_retry and result.status not in {"completed", "blocked", "incomplete_audit"}:
                    result = replace(result, status="incomplete_audit", text=(
                        result.text.rstrip() + f"\n\n审计协议补交未完成（{result.status}），没有可执行的返修结论。"))
                if (result.status == "completed" and (result.verdict or {}).get("verdict") == "PASS"
                        and any(((result.verdict or {}).get("reviewed_snapshots") or {}).get(key) != digest
                                for key, digest in required_reviews.items())):
                    # Keep the boundary even for custom runners that bypass
                    # SubmitVerdict. Never publish an unbound PASS edge.
                    result = replace(result, status="incomplete_audit", verdict=None, audit_snapshot=None,
                        text=result.text.rstrip() + "\n终校 PASS 未引用本轮上游审查快照，结论未生效；请补交有效的 reviewed_snapshots。")
                completed_at = time.time()
                if (node.role == "executor" or result.handoff) and result.status == "completed":
                    from mewcode.experts.completion import handoff_blocker
                    try:
                        blocker = await asyncio.to_thread(handoff_blocker, result.handoff,
                            work_dir=(policy_override.work_dir if policy_override else None) or self.work_dir)
                    except OSError as exc:
                        blocker = f"交付文件无法核验：{exc}"
                    if blocker:
                        result = replace(result, status="needs_attention", text=result.text.rstrip() + "\n" + blocker)
                if result.status != "completed" and not result.error_kind:
                    result = replace(result, error_kind=handoff_error_kind(result.handoff))
                current_receipts = list(saved.get("command_receipts") or []) if node.kind == "role" else []
                if self.run_store is not None and node.kind == "role":
                    live_stage = (((self.run_store.load(run_id) or {}).get("node_states") or {}).get(node.node_id) or {})
                    if live_stage.get("input_revision") == execution_policy.input_revision:
                        current_receipts = list(live_stage.get("command_receipts") or current_receipts)
                # A planner with ordinary tools may now execute its own accepted
                # request. Bind that actual host result instead of redispatching
                # the old request through a helper after the node's next handoff.
                from mewcode.experts.command_receipts import observe_requests, persist_command_receipts
                direct = []
                for row in current_receipts:
                    if (row.get("input_revision") != execution_policy.input_revision
                            or row.get("status") not in {"not_started", "not_dispatched"}):
                        continue
                    matches = [item for item in result.verification_records or []
                        if item.get("name") == "Bash" and str((item.get("args") or {}).get("command") or "").strip()
                        == str((row.get("request") or {}).get("command") or "").strip()
                        and (item.get("host_observation") or {}).get("command_execution")
                        and (self.run_store is None or ((live_stage.get("tools") or {}).get(item.get("tool_id")) or {}).get(
                            "node_attempt") == node_attempts[node.node_id])]
                    if matches:
                        direct.extend(observe_requests([{**row,
                            "dispatch_id": f"direct:{node.node_id}:{node_attempts[node.node_id]}"}], matches))
                if direct:
                    ledger = {"node_states": {node.node_id: {"command_receipts": current_receipts}}}
                    persist_command_receipts(ledger, {"node_id": node.node_id, "command_receipts": direct})
                    current_receipts = ledger["node_states"][node.node_id]["command_receipts"]
                    result = replace(result, command_receipts=current_receipts)
                if node.kind == "role" and not capability_followup_used:
                    from mewcode.experts.pending_commands import restore_pending_commands
                    result = restore_pending_commands(node.node_id, execution_policy.input_revision,
                        current_receipts, result)
                if node.kind == "role" and any((result.handoff or {}).get(key)
                                                for key in ("file_requests", "command_requests")):
                    from mewcode.experts.file_escalation import resolve_capability_requests
                    # Parent-owned assistance is separate from granting a
                    # read-only manager access to mutation tools.
                    command_request = bool((result.handoff or {}).get("command_requests"))
                    helper_suffix = "__command_probe" if command_request else "__file_delivery"
                    helper_label = " · 命令探测" if command_request else " · 文件交付"
                    helper_node = replace(node, node_id=node.node_id + helper_suffix,
                        role="executor", model=None, members=(),
                        label=node.label + helper_label)
                    helper_model, helper_route_reason = select_capability_executor_model(
                        catalog, parsed_profile.capability_executor
                    )

                    if helper_model is None:
                        requested_helper = parsed_profile.capability_executor
                        blocked_handoff = copy.deepcopy(result.handoff or {})
                        blocked_handoff["escalation_receipt"] = {
                            "status": "needs_attention",
                            "kind": "command" if command_request else "file",
                            "executor_route": {
                                "provider_id": (
                                    requested_helper.provider_key
                                    if requested_helper is not None else ""
                                ),
                                "model_id": (
                                    requested_helper.model_id
                                    if requested_helper is not None else ""
                                ),
                            },
                            "handoff": None,
                            "text": helper_route_reason,
                            "verification_records": [],
                            "error": helper_route_reason,
                        }
                        from mewcode.experts.command_receipts import request_receipts
                        command_receipts = request_receipts(node.node_id, execution_policy.input_revision,
                            (result.handoff or {}).get("command_requests") or [], reason=helper_route_reason,
                            epoch=execution_policy.node_epoch)
                        blocked_handoff["escalation_receipt"]["command_receipts"] = command_receipts
                        result = replace(result, status="needs_attention",
                            handoff=blocked_handoff, command_receipts=command_receipts, error_kind="capability_missing",
                            text=result.text.rstrip() + "\n" + helper_route_reason)
                        await self._emit({"type": "expert_flow_state", "run_id": run_id,
                            "phase": "capability_escalation", "node_id": node.node_id,
                            "status": "needs_attention", "reason": helper_route_reason,
                            "executor_model": (
                                requested_helper.model_id
                                if requested_helper is not None else ""
                            ),
                            "executor_provider": (
                                requested_helper.provider_key
                                if requested_helper is not None else ""
                            ),
                            "fallback": False})
                    else:

                        async def dispatch_capability_request(helper_policy, request_prompt):
                            dispatch_node = (replace(helper_node, node_id=node.node_id + "__execution_recovery",
                                label=node.label + " · 执行条件修复") if helper_policy.execution_recovery else helper_node)
                            return await call_runner(dispatch_node, helper_model,
                                REPORT_STYLE_CONTRACT + "\n原任务及授权约束：\n" + task
                                + "\n" + supplemental + "\n"
                                + json.dumps(flow.meta.get("shared_constraints") or {}, ensure_ascii=False)
                                + "\n原节点职责：" + node.responsibility + "\n" + request_prompt,
                                helper_policy)

                        async def emit_capability_request(event):
                            await self._emit({"type": "expert_flow_state", "run_id": run_id,
                                "executor_model": helper_model.model_id,
                                "executor_provider": helper_model.provider_key,
                                "fallback": False, **event})

                        remaining_timeout = execution_policy.timeout_seconds
                        if remaining_timeout is not None:
                            remaining_timeout = max(0.001, remaining_timeout - (time.time() - started_at))
                        if budget.deadline_seconds is not None:
                            global_remaining = max(0.001, budget.deadline_seconds - (time.monotonic() - started))
                            remaining_timeout = min(remaining_timeout, global_remaining) if remaining_timeout is not None else global_remaining
                        result = await resolve_capability_requests(node, result,
                            replace(execution_policy, timeout_seconds=remaining_timeout),
                            work_dir=execution_policy.work_dir or self.work_dir,
                            dispatch=dispatch_capability_request, emit=emit_capability_request,
                            prior_handoff=(saved.get("handoff") if isinstance(saved, dict) else None),
                            executor_route=helper_model.as_dict(),
                            previous_helper_state=saved_states.get(helper_node.node_id),
                            prior_command_receipts=current_receipts,
                            command_admission=self.command_admission,
                            recover_execution=self.execution_recovery_enabled,
                            allow_command_followup=not capability_followup_used)
                if (node.role == "auditor" and result.status == "completed" and result.verdict
                        and not result.audit_snapshot):
                    from mewcode.experts.stage_context import review_snapshot
                    result = replace(result, audit_snapshot=review_snapshot(run_id=run_id,
                        flow_version=flow.version, node_id=node.node_id, verdict=result.verdict))
                memory = summarize_attempt(result, attempt=node_attempts[node.node_id])
                history = attempt_histories.setdefault(node.node_id, [])
                history.append(memory)
                del history[:-3]
                record = {
                    "node_id": node.node_id,
                    "role": node.role,
                    "label": node.label,
                    "provider": provider_name,
                    "model": model_name,
                    "status": result.status,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "duration_ms": round((completed_at - started_at) * 1000),
                    "fallback_reason": fallback,
                    "node_attempt": node_attempts[node.node_id],
                    "recovery_round": recovery_round,
                    "transcript": result.text,
                    "verdict": result.verdict,
                    "loop_decision": result.loop_decision,
                    "handoff": result.handoff,
                    "audit_snapshot": result.audit_snapshot,
                    "attempt_history": list(history),
                    "verification_records": result.verification_records,
                    "command_receipts": result.command_receipts,
                    "activity_records": result.activity_records,
                }
                records.append(record)
                payload = {
                    key: value for key, value in record.items()
                    if key not in {"transcript", "verdict", "loop_decision"}
                }
                payload["execution_status"] = result.status
                pending_review = bool(policy_override is not None and (
                    policy_override.audit_contract is not None or policy_override.candidate_context is not None
                    or policy_override.loop_context is not None))
                if pending_review and result.status == "completed":
                    # A completed candidate/audit is provisional until the
                    # deterministic gate accepts its exact artifact version.
                    payload["status"] = "review_pending"
                await self._emit({
                    "type": "expert_node_completed",
                    "run_id": run_id,
                    **payload,
                    "transcript": result.text,
                    "text": result.text,
                    "verdict": result.verdict,
                    "loop_decision": result.loop_decision,
                    "passed": not pending_review and is_success(node, result),
                })
                await self._emit({
                    "type": "expert_stage",
                    "run_id": run_id,
                    **payload,
                    "attempt": attempt,
                })
                if result.status == "incomplete_audit" and audit_protocol_slot:
                    if audit_protocol_retry:
                        break
                    audit_protocol_retry = True
                    await self._emit({"type": "expert_flow_state", "run_id": run_id,
                        "phase": "audit_protocol_retry", "node_id": node.node_id,
                        "reason": "missing_submit_verdict", "retry": 1})
                    continue
                if audit_protocol_retry:
                    break
                if (result.error_kind == "capability_resolved"
                        and (result.handoff or {}).get("capability_followup_required")
                        and not capability_followup_used):
                    capability_followup_used = True
                    receipt = (result.handoff or {}).get("escalation_receipt") or {}
                    capability_results = (result.handoff or {}).get("capability_results") or {}
                    incoming.append({
                        "from_node": node.node_id + "__command_probe",
                        "to_node": node.node_id,
                        "role": "executor",
                        "label": "只读命令探测回填",
                        "condition": "capability_followup",
                        "status": "completed",
                        "text": str(receipt.get("text") or ""),
                        "handoff": {
                            "status": "completed",
                            "summary": capability_results.get("summary", ""),
                            "evidence": capability_results.get("evidence", []),
                            "verification_records": capability_results.get("verification_records", []),
                            "command_receipts": capability_results.get("command_receipts", []),
                        },
                    })
                    await self._emit({"type": "expert_flow_state", "run_id": run_id,
                        "phase": "capability_followup", "node_id": node.node_id,
                        "source_node_id": node.node_id + "__command_probe", "status": "running"})
                    continue
                if (is_success(node, result) or result.status in {"blocked", "review_pending"}
                        or result.error_kind in {"capability_missing", "permission_denied", "effect_unknown",
                                                 "environment_fault", "command_failed"}
                        or retry_index + 1 >= max_attempts):
                    break
                await self._emit({
                    "type": "expert_flow_state",
                    "run_id": run_id,
                    "phase": "retry",
                    "node_id": node.node_id,
                    "retry": retry_index + 1,
                })
            if (self.recovery_handler is not None and not (policy_override and policy_override.assistance_session)
                    and node.kind == "role"
                    and (result.status in {"continuing", "budget_exhausted", "stalled", "failed", "incomplete_audit"}
                         or result.error_kind in {"provider_interrupted", "capability_missing", "handoff_format",
                                                  "environment_fault", "command_failed", "effect_unknown", "assistance_requested"})):
                recovery_policy = policy_override or policy_for(node)
                consumed = result.billable_tokens if result.billable_tokens is not None else result.input_tokens + result.output_tokens
                remaining_tokens = None if recovery_policy.token_budget is None else recovery_policy.token_budget - consumed
                remaining_seconds = None if recovery_policy.timeout_seconds is None else max(
                    0., recovery_policy.timeout_seconds - (time.time() - started_at))
                if budget.deadline_seconds is not None:
                    global_remaining = max(0., budget.deadline_seconds - (time.monotonic() - started))
                    remaining_seconds = global_remaining if remaining_seconds is None else min(remaining_seconds, global_remaining)
                from mewcode.experts.recovery_state import advance_work_cursor
                from mewcode.experts.continuation import NodeContinuation, decision_prompt
                progressed, cursor = advance_work_cursor(result.work_progress or {}, work_cursor)
                productive = progressed and result.error_kind in {"internal_work", "segment_timeout", ""}
                allowed = ([] if over_deadline() else ["continue_checkpoint", "diagnose_path"])
                live_route_policy = (self.run_store.load(run_id) if self.run_store is not None else checkpoint) or {}
                if allowed and (node.backup_models or live_route_policy.get('difficult_node_model_policy')) and result.error_kind in {
                        "provider_interrupted", "model_stream_stalled", "provider_rate_limited",
                        "provider_temporarily_unavailable", "semantic_uncertain", "reasoning_insufficient",
                        "review_low_confidence", "idle_stall", "no_progress"}:
                    allowed.append("switch_declared_route")
                decision = await self.recovery_handler(node=node, result=result,
                    policy=replace(recovery_policy, token_budget=remaining_tokens, timeout_seconds=remaining_seconds,
                                   deny_tools=node.deny_tools), allowed=allowed,
                    host_facts={"trigger": result.error_kind or result.status,
                                "boundary_type": "continuation" if productive else "recovery",
                                "hard_boundary": ("全局运行时限已耗尽" if over_deadline()
                                    else "连续两次恢复没有新的工作进展" if not allowed else "")}, account=None)
                if decision and decision.get("action") in allowed and not over_deadline():
                    if remaining_tokens is not None:
                        remaining_tokens -= int(decision.get("decision_tokens", 0))
                    if remaining_seconds is not None:
                        remaining_seconds -= float(decision.get("decision_seconds", 0))
                    if (remaining_tokens is not None and remaining_tokens < 1024) or (remaining_seconds is not None and remaining_seconds <= 0):
                        return result
                    return NodeContinuation(result=result,
                        policy=replace(recovery_policy, resume_messages=True, token_budget=remaining_tokens,
                                                timeout_seconds=remaining_seconds,
                                                max_iterations=min(recovery_policy.max_iterations or 6,
                                                    int(decision.get("probe_max_iterations") or 6))),
                        prompt=continuation_base_prompt + decision_prompt(decision), cursor=cursor,
                        productive=productive and decision.get("source") == "host_progress_checkpoint")
            return result

        async def execute_runtime_expansion(
            parent: ExpertNodeSpec,
            parent_result: ExpertRoleResult,
            incoming: list[dict[str, Any]],
        ) -> ExpertRoleResult:
            """Run one immutable, single-level micrograph under its parent.

            This deliberately does not call ``execute_node`` for children:
            that path builds the full task prompt and can install delegation
            or expansion controls.  Micrograph children instead get a fresh,
            bounded session containing only their contract, declared inputs,
            and direct predecessor handoffs.
            """
            expansion = copy.deepcopy(parent_result.node_expansion or {})
            proposal = expansion.get("proposal") or {}
            expansion_id = str(expansion.get("expansion_id") or "")
            if not expansion_id or not isinstance(proposal, dict):
                return replace(
                    parent_result,
                    status="failed",
                    error_kind="node_expansion_invalid",
                    text="运行时微图缺少稳定身份或提案快照，已停止父节点。",
                )

            from mewcode.experts.node_expansion import FORBIDDEN_EXPANSION_TOOLS, JOIN_NODE_ID
            from mewcode.experts.stage_context import (
                collect_stage_inputs,
                runtime_prompt,
                stage_inputs_prompt,
            )
            from mewcode.tools.submit_handoff import fallback_handoff
            import hashlib

            local_nodes = {
                str(item.get("node_id")): dict(item)
                for item in proposal.get("nodes") or []
                if isinstance(item, dict) and item.get("node_id")
            }
            declaration_order = list(local_nodes)
            edges = [dict(item) for item in proposal.get("edges") or [] if isinstance(item, dict)]
            predecessors: dict[str, list[str]] = {key: [] for key in local_nodes}
            successors: dict[str, list[str]] = {key: [] for key in local_nodes}
            for edge in edges:
                source = str(edge.get("from_node") or "")
                target = str(edge.get("to_node") or "")
                if source in local_nodes and target in local_nodes:
                    predecessors[target].append(source)
                    successors[source].append(target)

            # Recompute a deterministic topological order from the frozen
            # proposal.  Validation already proved acyclicity; this guard
            # still fails closed for old/corrupt snapshots.
            indegree = {key: len(value) for key, value in predecessors.items()}
            ready = [key for key in declaration_order if indegree[key] == 0]
            ordered: list[str] = []
            while ready:
                current = ready.pop(0)
                ordered.append(current)
                for target in successors[current]:
                    indegree[target] -= 1
                    if indegree[target] == 0:
                        ready.append(target)
                ready.sort(key=declaration_order.index)
            if len(ordered) != len(local_nodes):
                return replace(
                    parent_result,
                    status="failed",
                    error_kind="node_expansion_invalid",
                    text="已保存的运行时微图拓扑无效，未执行任何子节点。",
                )

            model_ref, _ = model_for(parent)
            base_policy = policy_for(parent)

            def seal_parent_record(
                status: str, text: str, handoff: dict[str, Any]
            ) -> None:
                for row in reversed(records):
                    if row.get("node_id") == parent.node_id:
                        row.update(
                            status=status,
                            transcript=text,
                            handoff=copy.deepcopy(handoff),
                            completed_at=time.time(),
                        )
                        return
                records.append({
                    "node_id": parent.node_id,
                    "role": parent.role,
                    "label": parent.label,
                    "provider": model_ref.provider_key,
                    "model": model_ref.model_id,
                    "status": status,
                    "input_tokens": parent_result.input_tokens,
                    "output_tokens": parent_result.output_tokens,
                    "node_attempt": node_attempts.get(parent.node_id, 1),
                    "transcript": text,
                    "handoff": copy.deepcopy(handoff),
                    "completed_at": time.time(),
                })

            parent_sources = collect_stage_inputs(incoming, handoffs, results)
            parent_contract = {
                "node_id": parent.node_id,
                "label": parent.label,
                "role": parent.role,
                "responsibility": parent.responsibility
                or EXPERT_ROLE_CONTRACTS["executor"].responsibility,
                "shared_constraints": (flow.meta or {}).get("shared_constraints") or [],
                "node_constraints": ((flow.meta or {}).get("node_constraints") or {}).get(parent.node_id) or [],
            }
            parent_contract_text = json.dumps(parent_contract, ensure_ascii=False, sort_keys=True)
            overlay_saved = saved_expansions.get(parent.node_id) or {}
            saved_children = overlay_saved.get("children") or {}

            saved_join = overlay_saved.get("join") or {}
            saved_join_handoff = overlay_saved.get("join_handoff")
            if (
                isinstance(saved_join_handoff, dict)
                and saved_join_handoff.get("status") == "completed"
                and (
                    overlay_saved.get("status") == "completed"
                    or saved_join.get("status") == "completed"
                )
            ):
                # A crash can happen after the deterministic join was durably
                # written but before the parent completion event.  Reuse the
                # join receipt instead of charging or emitting a second join.
                if overlay_saved.get("status") != "completed":
                    await self._emit({
                        "type": "expert_node_expansion_completed",
                        "run_id": run_id,
                        "node_id": parent.node_id,
                        "role": parent.role,
                        "expansion_id": expansion_id,
                        "status": "completed",
                        "join_handoff": saved_join_handoff,
                        "restored": True,
                    })
                completed_parent = replace(
                    parent_result,
                    status="completed",
                    text=str(saved_join_handoff.get("summary") or "运行时微图已汇合。"),
                    handoff=copy.deepcopy(saved_join_handoff),
                    error_kind="",
                    node_expansion=expansion,
                    restored=True,
                )
                seal_parent_record(
                    "completed", completed_parent.text, saved_join_handoff
                )
                await self._emit({
                    "type": "expert_node_completed",
                    "run_id": run_id,
                    "node_id": parent.node_id,
                    "role": parent.role,
                    "label": parent.label,
                    "status": "completed",
                    "execution_status": "completed",
                    "completed_at": time.time(),
                    "node_attempt": node_attempts.get(parent.node_id, 1),
                    "provider": model_ref.provider_key,
                    "model": model_ref.model_id,
                    "input_tokens": parent_result.input_tokens,
                    "output_tokens": parent_result.output_tokens,
                    "billable_tokens": parent_result.billable_tokens,
                    "handoff": saved_join_handoff,
                    "transcript": completed_parent.text,
                    "text": completed_parent.text,
                    "passed": True,
                    "runtime_expansion": True,
                    "restored_join": True,
                })
                return completed_parent

            def used_tokens(value: ExpertRoleResult | dict[str, Any]) -> int:
                if isinstance(value, ExpertRoleResult):
                    billable = value.billable_tokens
                    return int(billable if billable is not None else value.input_tokens + value.output_tokens)
                billable = value.get("billable_tokens")
                return int(billable if billable is not None else int(value.get("input_tokens") or 0)
                           + int(value.get("output_tokens") or 0))

            def record_child(
                *, local_id: str, child_id: str, definition: dict[str, Any],
                result: ExpertRoleResult, revision: str, restored: bool = False,
                started_at: float | None = None, completed_at: float | None = None,
            ) -> None:
                records.append({
                    "node_id": child_id,
                    "parent_node_id": parent.node_id,
                    "local_id": local_id,
                    "expansion_id": expansion_id,
                    "role": "executor",
                    "label": str(definition.get("label") or local_id),
                    "provider": model_ref.provider_key,
                    "model": model_ref.model_id,
                    "status": result.status,
                    "input_revision": revision,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "billable_tokens": result.billable_tokens,
                    "started_at": started_at,
                    "completed_at": completed_at or time.time(),
                    "transcript": result.text,
                    "handoff": copy.deepcopy(result.handoff),
                    "restored": restored,
                    "runtime_expansion": True,
                })

            remaining_tokens = base_policy.token_budget
            if remaining_tokens is not None:
                remaining_tokens -= used_tokens(parent_result)
            consumed_active_seconds = max(0.0, float(parent_result.active_seconds or 0.0))
            expansion_started = time.monotonic()

            await self._emit({
                "type": "expert_node_expansion_activated",
                "run_id": run_id,
                "node_id": parent.node_id,
                "role": parent.role,
                "status": "running",
                "expansion_id": expansion_id,
                "proposal": proposal,
                "input_revision": str(expansion.get("input_revision") or overlay_saved.get("input_revision") or ""),
                "child_node_ids": [f"{parent.node_id}::{expansion_id}::{key}" for key in declaration_order],
                "join_node_id": f"{parent.node_id}::{expansion_id}::{JOIN_NODE_ID}",
            })

            child_results: dict[str, ExpertRoleResult] = {}
            child_revisions: dict[str, str] = {}
            async def execute_expansion_child(local_id, token_allocation, time_allocation):
                # The host reserves each sibling's share before dispatch. A
                # child cannot spend a second copy of the parent's remainder.
                remaining_tokens = token_allocation
                definition = local_nodes[local_id]
                child_id = f"{parent.node_id}::{expansion_id}::{local_id}"
                direct_inputs: dict[str, Any] = {}
                declared_paths: list[str] = []
                for ref in definition.get("input_refs") or []:
                    ref = str(ref)
                    if ref == "parent":
                        continue
                    if ref in parent_sources:
                        direct_inputs[ref] = copy.deepcopy(parent_sources[ref])
                    else:
                        declared_paths.append(ref)
                predecessor_rows: list[dict[str, Any]] = []
                for predecessor in predecessors[local_id]:
                    predecessor_result = child_results[predecessor]
                    predecessor_id = f"{parent.node_id}::{expansion_id}::{predecessor}"
                    full_handoff = copy.deepcopy(
                        predecessor_result.handoff
                        or fallback_handoff(predecessor_result.text)
                    )
                    direct_inputs[predecessor_id] = {
                        "node_id": predecessor_id,
                        "status": predecessor_result.status,
                        "text": str(predecessor_result.text)[:2000],
                        "handoff": full_handoff,
                    }
                    compact_handoff = {
                        "status": full_handoff.get("status"),
                        "summary": str(full_handoff.get("summary") or "")[:2000],
                        "artifacts": copy.deepcopy(full_handoff.get("artifacts") or [])[:32],
                        "decisions": [str(item)[:600] for item in (full_handoff.get("decisions") or [])[:8]],
                        "open_items": [str(item)[:600] for item in (full_handoff.get("open_items") or [])[:8]],
                        "evidence": copy.deepcopy(full_handoff.get("evidence") or [])[:8],
                        "inline_outputs": [
                            {
                                "name": str(item.get("name") or "")[:200],
                                "characters": len(str(item.get("content") or "")),
                            }
                            for item in (full_handoff.get("inline_outputs") or [])[:32]
                            if isinstance(item, dict)
                        ],
                    }
                    predecessor_rows.append({
                        "node_id": predecessor_id,
                        "local_id": predecessor,
                        "status": predecessor_result.status,
                        "handoff": compact_handoff,
                    })
                revision_body = {
                    "expansion_id": expansion_id,
                    "parent_input_revision": str(expansion.get("input_revision") or overlay_saved.get("input_revision") or ""),
                    "node": definition,
                    "declared_inputs": direct_inputs,
                    "declared_paths": declared_paths,
                    "predecessors": predecessor_rows,
                }
                child_revision = hashlib.sha256(json.dumps(
                    revision_body, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                child_revisions[local_id] = child_revision

                saved_child = saved_states.get(child_id) or saved_children.get(local_id) or {}
                if (
                    isinstance(saved_child, dict)
                    and saved_child.get("status") == "completed"
                    and saved_child.get("input_revision") == child_revision
                ):
                    restored = ExpertRoleResult(
                        text=str(saved_child.get("text") or saved_child.get("transcript") or ""),
                        input_tokens=int(saved_child.get("input_tokens") or 0),
                        output_tokens=int(saved_child.get("output_tokens") or 0),
                        billable_tokens=(int(saved_child.get("billable_tokens"))
                                         if saved_child.get("billable_tokens") is not None else None),
                        active_seconds=float(saved_child.get("active_seconds") or 0.0),
                        status="completed",
                        handoff=copy.deepcopy(saved_child.get("handoff")),
                        restored=True,
                    )
                    child_results[local_id] = restored
                    record_child(
                        local_id=local_id,
                        child_id=child_id,
                        definition=definition,
                        result=restored,
                        revision=child_revision,
                        restored=True,
                    )
                    await self._emit({
                        "type": "expert_expansion_child_completed",
                        "run_id": run_id,
                        "node_id": child_id,
                        "parent_node_id": parent.node_id,
                        "role": "executor",
                        "label": str(definition.get("label") or local_id),
                        "local_id": local_id,
                        "expansion_id": expansion_id,
                        "status": "completed",
                        "input_revision": child_revision,
                        "handoff": restored.handoff,
                        "restored": True,
                    })
                    return None

                out_of_time = time_allocation is not None and time_allocation <= 0
                if over_deadline() or out_of_time or (remaining_tokens is not None and remaining_tokens < 1024):
                    child_result = ExpertRoleResult(
                        text=(RUN_DEADLINE_ERROR if over_deadline()
                              else "父节点时间预算已耗尽，未启动该微图子节点。" if out_of_time
                              else "父节点剩余 Token 预算不足，未启动该微图子节点。"),
                        status="budget_exhausted",
                        error_kind="run_deadline" if over_deadline() else "time_budget" if out_of_time else "token_budget",
                    )
                    child_results[local_id] = child_result
                    record_child(
                        local_id=local_id,
                        child_id=child_id,
                        definition=definition,
                        result=child_result,
                        revision=child_revision,
                    )
                    budget_payload = {
                        "run_id": run_id,
                        "node_id": child_id,
                        "parent_node_id": parent.node_id,
                        "role": "executor",
                        "label": str(definition.get("label") or local_id),
                        "local_id": local_id,
                        "expansion_id": expansion_id,
                        "status": child_result.status,
                        "execution_status": child_result.status,
                        "completed_at": time.time(),
                        "node_attempt": node_attempts.get(child_id, 0),
                        "input_revision": child_revision,
                        "provider": model_ref.provider_key,
                        "model": model_ref.model_id,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "billable_tokens": 0,
                        "handoff": fallback_handoff(child_result.text),
                        "transcript": child_result.text,
                        "text": child_result.text,
                        "passed": False,
                        "runtime_expansion": True,
                    }
                    await self._emit({
                        "type": "expert_expansion_child_completed", **budget_payload
                    })
                    await self._emit({"type": "expert_node_completed", **budget_payload})
                else:
                    child_node = ExpertNodeSpec(
                        node_id=child_id,
                        role="executor",
                        label=str(definition.get("label") or local_id),
                        responsibility=str(definition.get("task") or ""),
                        model=model_ref,
                        tools=tuple(str(item) for item in definition.get("tools") or []),
                        deny_tools=tuple(sorted(FORBIDDEN_EXPANSION_TOOLS)),
                    )
                    node_attempts[child_id] = node_attempts.get(child_id, 0) + 1
                    child_started = time.time()
                    await self._emit({
                        "type": "expert_expansion_child_started",
                        "run_id": run_id,
                        "node_id": child_id,
                        "parent_node_id": parent.node_id,
                        "role": "executor",
                        "label": child_node.label,
                        "local_id": local_id,
                        "expansion_id": expansion_id,
                        "status": "running",
                        "started_at": child_started,
                        "node_attempt": node_attempts[child_id],
                        "input_revision": child_revision,
                        "provider": model_ref.provider_key,
                        "model": model_ref.model_id,
                    })
                    await self._emit({
                        "type": "expert_node_started",
                        "run_id": run_id,
                        "node_id": child_id,
                        "parent_node_id": parent.node_id,
                        "role": "executor",
                        "label": child_node.label,
                        "status": "running",
                        "started_at": child_started,
                        "node_attempt": node_attempts[child_id],
                        "input_revision": child_revision,
                        "provider": model_ref.provider_key,
                        "model": model_ref.model_id,
                        "effort": model_ref.effort,
                        "runtime_expansion": True,
                    })
                    timeout = time_allocation
                    if budget.deadline_seconds is not None:
                        global_remaining = max(
                            0.001,
                            budget.deadline_seconds - (time.monotonic() - started),
                        )
                        timeout = min(timeout, global_remaining) if timeout is not None else global_remaining
                    child_policy = replace(
                        base_policy,
                        # Expansion children retain the explicitly selected
                        # authorization principal. Never infer it from the
                        # child ID, whose spelling is only an execution ID.
                        authorization_node_id=base_policy.authorization_node_id or parent.node_id,
                        allow_delegation=False,
                        max_direct_agents=0,
                        allow_recursive_delegation=False,
                        resume_messages=True,
                        audit_contract=None,
                        loop_context=None,
                        expansion_holder=None,
                        expansion_parent_contract=parent_contract_text,
                        expansion_write_paths=tuple(
                            (expansion.get("normalized_write_paths") or {}).get(local_id)
                            or definition.get("write_paths") or []
                        ),
                        token_budget=remaining_tokens,
                        timeout_seconds=timeout,
                        handoff_holder=None,
                        stage_inputs=direct_inputs,
                        input_revision=child_revision,
                        required_review_snapshots=None,
                        node_tools=child_node.tools,
                        deny_tools=child_node.deny_tools,
                    )
                    child_contract = {
                        "parent_contract": parent_contract,
                        "child": {
                            "node_id": child_id,
                            "local_id": local_id,
                            "label": child_node.label,
                            "task": child_node.responsibility,
                            "declared_input_refs": list(definition.get("input_refs") or []),
                            "workspace_input_paths": declared_paths,
                            "output_contract": list(definition.get("outputs") or []),
                            "acceptance": list(definition.get("acceptance") or []),
                            "write_paths": list(definition.get("write_paths") or []),
                        },
                        "direct_predecessor_handoffs": predecessor_rows,
                    }
                    child_prompt = (
                        REPORT_STYLE_CONTRACT
                        + "\n\n你是单层运行时微图的实现子节点。"
                        "仅执行下列合同；不得委派、创建团队或再次提案展开。\n"
                        "MICROGRAPH CHILD CONTRACT:\n"
                        + json.dumps(child_contract, ensure_ascii=False)
                    )
                    if direct_inputs:
                        child_prompt += stage_inputs_prompt(direct_inputs)
                    child_prompt += (
                        "\n\n结束前必须调用 SubmitHandoff，仅提交结构化摘要、"
                        "产物引用、决定、未决项和证据；不要复制完整聊天。"
                    )
                    child_prompt += runtime_prompt(
                        run_id=run_id,
                        flow=flow,
                        node_id=child_id,
                        model=model_ref,
                        work_dir=base_policy.work_dir or self.work_dir,
                        attempt=node_attempts[child_id],
                    )
                    try:
                        call = call_runner(
                            child_node, model_ref, child_prompt, child_policy
                        )
                        child_result = (
                            await asyncio.wait_for(call, timeout=timeout)
                            if timeout is not None else await call
                        )
                    except asyncio.CancelledError:
                        raise
                    except TimeoutError:
                        child_result = ExpertRoleResult(
                            text="微图子节点超过父节点剩余时间预算。",
                            status="budget_exhausted",
                            error_kind="segment_timeout",
                        )
                    except Exception as exc:
                        child_result = ExpertRoleResult(
                            text=format_error(exc), status="failed"
                        )
                    child_completed = time.time()
                    if child_result.status == "completed":
                        from mewcode.experts.completion import handoff_blocker
                        blocker = await asyncio.to_thread(
                            handoff_blocker,
                            child_result.handoff,
                            work_dir=base_policy.work_dir or self.work_dir,
                        )
                        if blocker:
                            child_result = replace(
                                child_result,
                                status="needs_attention",
                                text=child_result.text.rstrip() + "\n" + blocker,
                            )
                    if child_result.handoff is None:
                        child_result = replace(
                            child_result,
                            handoff=fallback_handoff(child_result.text),
                        )
                    child_results[local_id] = child_result
                    record_child(
                        local_id=local_id,
                        child_id=child_id,
                        definition=definition,
                        result=child_result,
                        revision=child_revision,
                        started_at=child_started,
                        completed_at=child_completed,
                    )
                    completed_payload = {
                        "run_id": run_id,
                        "node_id": child_id,
                        "parent_node_id": parent.node_id,
                        "role": "executor",
                        "label": child_node.label,
                        "local_id": local_id,
                        "expansion_id": expansion_id,
                        "status": child_result.status,
                        "execution_status": child_result.status,
                        "started_at": child_started,
                        "completed_at": child_completed,
                        "duration_ms": round((child_completed - child_started) * 1000),
                        "node_attempt": node_attempts[child_id],
                        "input_revision": child_revision,
                        "provider": model_ref.provider_key,
                        "model": model_ref.model_id,
                        "input_tokens": child_result.input_tokens,
                        "output_tokens": child_result.output_tokens,
                        "billable_tokens": child_result.billable_tokens,
                        "active_seconds": child_result.active_seconds,
                        "handoff": child_result.handoff,
                        "transcript": child_result.text,
                        "text": child_result.text,
                        "passed": child_result.status == "completed",
                        "runtime_expansion": True,
                    }
                    await self._emit({
                        "type": "expert_expansion_child_completed",
                        **completed_payload,
                    })
                    await self._emit({
                        "type": "expert_node_completed", **completed_payload
                    })
                    await self._emit({
                        "type": "expert_stage", **{
                            key: value for key, value in completed_payload.items()
                            if key not in {"transcript", "text", "passed"}
                        }, "attempt": attempt,
                    })

                current = child_results[local_id]
                if current.status != "completed":
                    join_id = f"{parent.node_id}::{expansion_id}::{JOIN_NODE_ID}"
                    await self._emit({
                        "type": "expert_expansion_join_blocked",
                        "run_id": run_id,
                        "node_id": join_id,
                        "parent_node_id": parent.node_id,
                        "role": "join",
                        "label": str((proposal.get("join") or {}).get("label") or "汇合"),
                        "expansion_id": expansion_id,
                        "status": "blocked",
                        "blocked_by": child_id,
                        "child_status": current.status,
                    })
                    await self._emit({
                        "type": "expert_node_expansion_blocked",
                        "run_id": run_id,
                        "node_id": parent.node_id,
                        "role": parent.role,
                        "expansion_id": expansion_id,
                        "status": "blocked",
                        "blocked_by": child_id,
                        "child_status": current.status,
                    })
                    blocked_handoff = {
                        "status": "needs_attention",
                        "summary": f"微图子节点 {local_id} 未完成，宿主未执行汇合。",
                        "blockers": [f"{child_id}: {current.status}"],
                        "artifacts": [],
                        "decisions": [],
                        "open_items": [str(current.text)[:1000]],
                        "evidence": [],
                    }
                    blocked_parent = replace(
                        parent_result,
                        status="blocked",
                        error_kind=current.error_kind or "node_expansion_child_failed",
                        text=blocked_handoff["summary"],
                        handoff=blocked_handoff,
                        node_expansion=expansion,
                    )
                    seal_parent_record(
                        "blocked", blocked_parent.text, blocked_handoff
                    )
                    await self._emit({
                        "type": "expert_node_completed",
                        "run_id": run_id,
                        "node_id": parent.node_id,
                        "role": parent.role,
                        "label": parent.label,
                        "status": "blocked",
                        "execution_status": "blocked",
                        "completed_at": time.time(),
                        "node_attempt": node_attempts.get(parent.node_id, 1),
                        "provider": model_ref.provider_key,
                        "model": model_ref.model_id,
                        "input_tokens": parent_result.input_tokens,
                        "output_tokens": parent_result.output_tokens,
                        "billable_tokens": parent_result.billable_tokens,
                        "handoff": blocked_handoff,
                        "transcript": blocked_parent.text,
                        "text": blocked_parent.text,
                        "passed": False,
                        "runtime_expansion": True,
                    })
                    return blocked_parent

            # Run independent read-only branches together. Arbitrary commands,
            # MCP/browser state and write-capable scopes remain serial: declared
            # write paths alone do not constrain everything those tools can do.
            from mewcode.experts.parallel import expansion_wave, allocate_wave_tokens
            pending_children = list(ordered)
            while pending_children:
                ready = [key for key in pending_children if all(pred in child_results for pred in predecessors[key])]
                wave = expansion_wave(ready, local_nodes)
                if not wave:
                    raise ValueError("Micrograph has no runnable frontier")
                floors = []
                for key in wave:
                    child_id = f"{parent.node_id}::{expansion_id}::{key}"
                    old = saved_states.get(child_id) or saved_children.get(key) or {}
                    floors.append(int(old.get("billable_tokens") if old.get("billable_tokens") is not None else
                        int(old.get("input_tokens") or 0) + int(old.get("output_tokens") or 0)) if old.get("status") == "completed" else 0)
                allocations = allocate_wave_tokens(remaining_tokens, floors)
                if len(wave) > 1 and allocations is not None and any(cap < 1024 for cap in allocations):
                    wave, floors = wave[:1], floors[:1]
                    allocations = allocate_wave_tokens(remaining_tokens, floors)
                timeout = base_policy.timeout_seconds
                if timeout is not None:
                    timeout = max(0.0, timeout - consumed_active_seconds - (time.monotonic() - expansion_started))
                jobs = [asyncio.create_task(execute_expansion_child(key,
                    allocations[i] if allocations is not None else None, timeout)) for i, key in enumerate(wave)]
                try:
                    failures = await asyncio.gather(*jobs)
                finally:
                    for job in jobs:
                        if not job.done():
                            job.cancel()
                    await asyncio.gather(*jobs, return_exceptions=True)
                for key in wave:
                    value = child_results[key]
                    if remaining_tokens is not None:
                        remaining_tokens -= used_tokens(value)
                    if value.restored:
                        consumed_active_seconds += max(0.0, value.active_seconds)
                    pending_children.remove(key)
                failure = next((value for value in failures if value is not None), None)
                if failure is not None:
                    return failure

            join_id = f"{parent.node_id}::{expansion_id}::{JOIN_NODE_ID}"
            await self._emit({
                "type": "expert_expansion_join_started",
                "run_id": run_id,
                "node_id": join_id,
                "parent_node_id": parent.node_id,
                "role": "join",
                "label": str((proposal.get("join") or {}).get("label") or "汇合"),
                "expansion_id": expansion_id,
                "status": "joining",
                "required_child_node_ids": [
                    f"{parent.node_id}::{expansion_id}::{key}" for key in declaration_order
                ],
            })
            child_handoffs = []
            artifacts: list[dict[str, Any]] = []
            decisions: list[str] = []
            open_items: list[str] = []
            evidence: list[dict[str, Any]] = []
            seen_artifacts: set[str] = set()
            seen_evidence: set[str] = set()
            for local_id in declaration_order:
                value = child_results[local_id]
                handoff = value.handoff or fallback_handoff(value.text)
                child_handoffs.append({
                    "node_id": f"{parent.node_id}::{expansion_id}::{local_id}",
                    "local_id": local_id,
                    "status": value.status,
                    "summary": str(handoff.get("summary") or value.text)[:2000],
                    "declared_outputs": list(local_nodes[local_id].get("outputs") or []),
                    "acceptance": list(local_nodes[local_id].get("acceptance") or []),
                    "artifacts": copy.deepcopy(handoff.get("artifacts") or []),
                    "decisions": [str(item)[:2000] for item in (handoff.get("decisions") or [])],
                    "open_items": [str(item)[:2000] for item in (handoff.get("open_items") or [])],
                    "evidence": copy.deepcopy(handoff.get("evidence") or []),
                })
                for artifact in handoff.get("artifacts") or []:
                    key = json.dumps(artifact, ensure_ascii=False, sort_keys=True)
                    if key not in seen_artifacts:
                        seen_artifacts.add(key)
                        artifacts.append(copy.deepcopy(artifact))
                decisions.extend(str(item)[:2000] for item in handoff.get("decisions") or [])
                open_items.extend(str(item)[:2000] for item in handoff.get("open_items") or [])
                for item in handoff.get("evidence") or []:
                    key = json.dumps(item, ensure_ascii=False, sort_keys=True)
                    if key not in seen_evidence:
                        seen_evidence.add(key)
                        evidence.append(copy.deepcopy(item))
            join_handoff = {
                "status": "completed",
                "summary": f"运行时微图 {expansion_id} 的 {len(child_handoffs)} 个必需子节点已完成并确定性汇合。",
                "artifacts": artifacts[:32],
                "decisions": decisions[:32],
                "open_items": open_items[:32],
                "evidence": evidence[:16],
                "children": child_handoffs,
                "join_acceptance": list((proposal.get("join") or {}).get("acceptance") or []),
                "expansion_id": expansion_id,
            }
            await self._emit({
                "type": "expert_expansion_join_completed",
                "run_id": run_id,
                "node_id": join_id,
                "parent_node_id": parent.node_id,
                "role": "join",
                "label": str((proposal.get("join") or {}).get("label") or "汇合"),
                "expansion_id": expansion_id,
                "status": "completed",
                "join_handoff": join_handoff,
                "child_input_revisions": child_revisions,
            })
            await self._emit({
                "type": "expert_node_expansion_completed",
                "run_id": run_id,
                "node_id": parent.node_id,
                "role": parent.role,
                "expansion_id": expansion_id,
                "status": "completed",
                "join_handoff": join_handoff,
            })
            completed_parent = replace(
                parent_result,
                status="completed",
                text=join_handoff["summary"],
                handoff=join_handoff,
                error_kind="",
                node_expansion=expansion,
            )
            seal_parent_record(
                "completed", completed_parent.text, join_handoff
            )
            parent_completed_at = time.time()
            parent_payload = {
                "run_id": run_id,
                "node_id": parent.node_id,
                "role": parent.role,
                "label": parent.label,
                "status": "completed",
                "execution_status": "completed",
                "completed_at": parent_completed_at,
                "node_attempt": node_attempts.get(parent.node_id, 1),
                "provider": model_ref.provider_key,
                "model": model_ref.model_id,
                "input_tokens": parent_result.input_tokens,
                "output_tokens": parent_result.output_tokens,
                "billable_tokens": parent_result.billable_tokens,
                "handoff": join_handoff,
                "transcript": completed_parent.text,
                "text": completed_parent.text,
                "passed": True,
                "runtime_expansion": True,
            }
            await self._emit({"type": "expert_node_completed", **parent_payload})
            await self._emit({
                "type": "expert_stage",
                **{key: value for key, value in parent_payload.items()
                   if key not in {"transcript", "text", "passed"}},
                "attempt": attempt,
            })
            return completed_parent

        def input_route_matches(edge: Any) -> bool:
            if edge.input_when is None:
                return True
            from mewcode.orchestration.run_inputs import predicate_matches
            latest = (self.run_store.load(run_id) if self.run_store is not None and hasattr(self.run_store, "load") else None) or checkpoint
            values = latest.get("run_inputs", {})
            if edge.branch_group:
                choices = {item.input_when["equals"].strip() for item in flow.edges
                           if item.branch_group == edge.branch_group and item.input_when is not None}
                value = values.get(edge.input_when["input"], "")
                if not isinstance(value, str) or value.strip() not in choices:
                    raise ValueError(f"互斥分支参数「{edge.input_when['input']}」尚未选择有效路径，后续节点未执行")
            return predicate_matches(edge.input_when, values)

        async def route_matches(
            edge: Any,
            source_node: ExpertNodeSpec,
            result: ExpertRoleResult,
        ) -> bool:
            if not input_route_matches(edge):
                return False
            router = routers.get(edge.router)
            if callable(router):
                value = router(edge.condition, source_node, result, results)
                if inspect.isawaitable(value):
                    value = await value
                return bool(value)
            return edge_condition_matches(
                edge.condition,
                passed=is_success(source_node, result),
                status=str(result.status or ""),
            )

        def _has_fail_branch(node_id: str) -> bool:
            return any(
                normalize_edge_condition(edge.condition) == "fail"
                for edge in flow.edges
                if edge.from_node == node_id
            )

        async def publish_handoffs(node: ExpertNodeSpec, result: ExpertRoleResult) -> None:
            # Legacy completed audits predate durable review receipts. Freeze
            # their actual verdict in memory; do not rerun a model or invent data.
            if node.role == "auditor" and result.verdict and not result.audit_snapshot:
                from mewcode.experts.stage_context import review_snapshot
                result.audit_snapshot = review_snapshot(run_id=run_id, flow_version=flow.version,
                    node_id=node.node_id, verdict=result.verdict)
            for edge in flow.edges:
                if edge.from_node != node.node_id:
                    continue
                if not await route_matches(edge, node, result):
                    continue
                handoff = {
                    "from_node": node.node_id,
                    "to_node": edge.to_node,
                    "role": node.role,
                    "label": node.label,
                    "condition": edge.condition,
                    "text": result.text,
                    "status": result.status,
                    "created_at": time.time(),
                    "handoff": result.handoff,
                    "verdict": result.verdict,
                    "audit_snapshot": result.audit_snapshot,
                }
                handoffs.setdefault(edge.to_node, []).append(handoff)
                await self._emit({
                    "type": "expert_handoff",
                    "run_id": run_id,
                    **{key: value for key, value in handoff.items() if key != "text"},
                    "summary": result.text[:1200],
                })

        def recovery_path(start_id: str, end_id: str) -> list[str]:
            queue: list[tuple[str, list[str]]] = [(start_id, [start_id])]
            visited: set[str] = set()
            while queue:
                current, path = queue.pop(0)
                if current == end_id:
                    return path
                if current in visited:
                    continue
                visited.add(current)
                queue.extend(
                    (edge.to_node, path + [edge.to_node])
                    for edge in flow.edges
                    if edge.from_node == current and edge.to_node not in visited
                )
            return [start_id]

        async def recover_routed_node(
            failed_node: ExpertNodeSpec,
            failed_result: ExpertRoleResult,
        ) -> ExpertRoleResult:
            """Run a finite repair path and then re-run the failed audit.

            Canvas loops cannot live in the schedulable DAG.  Their back-edge
            is compiled into failure_policy.target_node_id, so recovery must
            explicitly return to the failed node before normal pass routing
            can continue.
            """
            policy = failed_node.failure_policy
            current = failed_result
            if policy.action == "fallback":
                targets = policy.fallback_node_ids or ((policy.target_node_id,) if policy.target_node_id else ())
                origin = (self.run_store.load(run_id) if self.run_store is not None else None) or checkpoint
                from mewcode.experts.node_ledger import ledger_for_run
                from dataclasses import asdict
                view = ledger_for_run(origin).view(failed_node.node_id)
                for target_id in targets:
                    fallback_node = flow.node_map[target_id]
                    await self._emit({"type": "expert_flow_state", "run_id": run_id, "phase": "fallback",
                        "node_id": failed_node.node_id, "target_node_id": target_id})
                    recovery_inputs = [*handoffs.get(failed_node.node_id, []), {
                        "from_node": failed_node.node_id, "to_node": target_id, "label": "回退输入与原节点事实",
                        "status": current.status, "text": current.text, "handoff": current.handoff,
                        "ledger": {"epoch": view.epoch, "facts": [asdict(f) for f in view.history]}}]
                    recovered = await execute_node(fallback_node, recovery_inputs, recovery_round=1)
                    results[target_id] = recovered
                    skipped_schedule_ids.add(target_id)
                    if is_success(fallback_node, recovered):
                        alternatives = set(targets) - {target_id}
                        inactive_node_ids.update(alternatives)
                        skipped_schedule_ids.update(alternatives)
                        await publish_handoffs(fallback_node, recovered)
                        if self.run_store is not None:
                            def save_fallback(record):
                                record.setdefault('fallback_resolutions', {})[failed_node.node_id] = {
                                    'selected_node_id': target_id, 'inactive_alternatives': sorted(alternatives)}
                                stage = record.setdefault('node_states', {}).setdefault(failed_node.node_id, {})
                                stage.update(status='completed', handoff=recovered.handoff,
                                             fallback_source_node_id=target_id)
                                ledger_for_run(record).record(failed_node.node_id, 'maintenance',
                                    {'fallback_selected': target_id, 'inactive_alternatives': sorted(alternatives)})
                            self.run_store.mutate(run_id, save_fallback, durable=True)
                        return recovered
                    current = recovered
                    from mewcode.experts.blocking_policy import BLOCKER_CLASS
                    if (BLOCKER_CLASS.get(recovered.error_kind) in {'authority', 'business'}
                            or recovered.status == 'paused'
                            or (recovered.status == 'blocked' and BLOCKER_CLASS.get(recovered.error_kind) != 'auto')):
                        return recovered
                return replace(current, status="needs_attention", text=current.text + "\n所有声明回退节点均未完成。")
            previous_issue_count = len(
                (current.verdict or {}).get("issues") or []
            )

            def stable_issue_ids(result: ExpertRoleResult) -> set[str] | None:
                issues = (result.verdict or {}).get("issues") or []
                if not issues:
                    return set()
                ids = {
                    str(item.get("issue_id") or "").strip()
                    for item in issues
                    if isinstance(item, dict)
                }
                if len(ids) != len(issues) or "" in ids:
                    return None
                return ids

            previous_issue_ids = stable_issue_ids(current)

            def stopped_result(
                message: str, last: ExpertRoleResult | None = None
            ) -> ExpertRoleResult:
                parts = [current.text.rstrip()]
                if last is not None and last is not current:
                    tail = (last.text or "").rstrip()
                    if tail:
                        parts.append(tail)
                text = "\n\n".join(part for part in parts if part)
                if text:
                    text += "\n\n"
                text += "自动修复已停止：" + message
                return replace(current, text=text, status="needs_attention")

            for recovery_round in range(1, policy.max_attempts + 1):
                await self._emit({
                    "type": "expert_flow_state",
                    "run_id": run_id,
                    "phase": "recovery",
                    "node_id": failed_node.node_id,
                    "target_node_id": policy.target_node_id,
                    "recovery_round": recovery_round,
                })
                path = ([policy.target_node_id] if policy.action == "fallback" else recovery_path(
                    policy.target_node_id, failed_node.node_id
                ))
                if policy.action != "fallback" and (not path or path[-1] != failed_node.node_id):
                    path.append(failed_node.node_id)
                prior = {
                    "from_node": failed_node.node_id,
                    "to_node": path[0],
                    "label": "恢复任务",
                    "text": current.text,
                    "status": current.status,
                    "handoff": current.handoff,
                }
                for recovery_node_id in path:
                    recovery_node = flow.node_map[recovery_node_id]
                    await self._emit({
                        "type": "expert_handoff",
                        "run_id": run_id,
                        "from_node": prior["from_node"],
                        "to_node": recovery_node_id,
                        "role": failed_node.role,
                        "label": prior["label"],
                        "condition": "recovery",
                        "status": prior["status"],
                        "created_at": time.time(),
                        "recovery_round": recovery_round,
                    })
                    previous_result = results.get(recovery_node_id)
                    recovery_inputs = [prior, *handoffs.get(recovery_node_id, [])]
                    if previous_result is not None:
                        recovery_inputs.append({"from_node": recovery_node_id, "label": "本节点上次交接",
                            "text": previous_result.text[:600], "handoff": previous_result.handoff})
                    recovered = await execute_node(recovery_node, recovery_inputs, recovery_round=recovery_round)
                    results[recovery_node_id] = recovered
                    if recovered.status == "blocked":
                        return recovered
                    prior = {
                        "from_node": recovery_node_id,
                        "to_node": failed_node.node_id,
                        "label": recovery_node.label,
                        "text": recovered.text,
                        "status": recovered.status,
                        "handoff": recovered.handoff,
                    }
                    if (
                        recovery_node_id != failed_node.node_id
                        and not is_success(recovery_node, recovered)
                    ):
                        await self._emit({
                            "type": "expert_flow_state",
                            "run_id": run_id,
                            "phase": "recovery_stalled",
                            "node_id": failed_node.node_id,
                            "target_node_id": recovery_node_id,
                            "recovery_round": recovery_round,
                            "reason": "repair_node_not_successful",
                        })
                        return stopped_result(
                            f"修复节点 {recovery_node.label} 未成功完成，需要人工确认。",
                            last=recovered,
                        )
                if policy.action == "fallback":
                    skipped_schedule_ids.add(policy.target_node_id)
                    return recovered
                current = results.get(failed_node.node_id, current)
                if current.status == "incomplete_audit":
                    return current
                if is_success(failed_node, current):
                    return current
                current_issue_count = len(
                    (current.verdict or {}).get("issues") or []
                )
                current_issue_ids = stable_issue_ids(current)
                ids_prove_progress = (
                    current_issue_count > 0
                    and
                    previous_issue_ids is not None
                    and current_issue_ids is not None
                    and current_issue_ids < previous_issue_ids
                )
                counts_prove_progress = (
                    (previous_issue_ids is None or current_issue_ids is None)
                    and 0 < current_issue_count < previous_issue_count
                )
                resolved = (previous_issue_ids - current_issue_ids) if previous_issue_ids is not None and current_issue_ids is not None else set()
                new_issues = (current_issue_ids - previous_issue_ids) if previous_issue_ids is not None and current_issue_ids is not None else set()
                net_progress = len(resolved) > len(new_issues) and recovery_round < policy.max_attempts
                # New findings are permitted only with net measured progress;
                # equal-size relabelling or regression still stops fail-closed.
                if not (ids_prove_progress or counts_prove_progress or net_progress):
                    await self._emit({
                        "type": "expert_flow_state",
                        "run_id": run_id,
                        "phase": "recovery_stalled",
                        "node_id": failed_node.node_id,
                        "target_node_id": policy.target_node_id,
                        "recovery_round": recovery_round,
                        "previous_issue_count": previous_issue_count,
                        "current_issue_count": current_issue_count,
                        "resolved_issue_count": len(resolved), "new_issue_count": len(new_issues),
                        "reason": "unresolved_issues_not_decreasing",
                    })
                    return stopped_result(
                        "复审未证明收敛（未解决问题数"
                        f" {previous_issue_count} → {current_issue_count}），"
                        "为避免死循环已转人工处理。"
                    )
                previous_issue_count = current_issue_count
                previous_issue_ids = current_issue_ids
            await self._emit({
                "type": "expert_flow_state",
                "run_id": run_id,
                "phase": "recovery_exhausted",
                "node_id": failed_node.node_id,
                "target_node_id": policy.target_node_id,
                "max_attempts": policy.max_attempts,
                "reason": "recovery_budget_exhausted",
            })
            return stopped_result(
                f"已达到 {policy.max_attempts} 轮修复上限，仍未获得 PASS。"
            )

        def _predecessors_satisfied(flow: ExpertFlowSpec, node_id: str, results: dict) -> bool:
            incoming = [edge for edge in flow.edges if edge.to_node == node_id]
            if not incoming:
                return True

            def pred_ok(edge: Any, *, require_success: bool) -> bool:
                if edge.from_node in inactive_node_ids:
                    return require_success and normalize_edge_condition(edge.condition) == "always"
                pred_result = results.get(edge.from_node)
                pred_node = flow.node_map.get(edge.from_node)
                if pred_result is None or pred_node is None:
                    return False
                if not input_route_matches(edge):
                    return False
                matched = edge_condition_matches(
                    edge.condition,
                    passed=is_success(pred_node, pred_result),
                    status=str(pred_result.status or ""),
                ) or (edge.router != "default" and any(
                    item.get("from_node") == edge.from_node and item.get("condition") == edge.condition
                    for item in handoffs.get(node_id) or []))
                if not matched:
                    return False
                if not require_success:
                    return True
                if is_success(pred_node, pred_result):
                    return True
                return (
                    pred_node.role == "auditor"
                    and pred_result.status == "completed"
                    and _has_fail_branch(pred_node.node_id)
                )

            always_edges = [
                edge
                for edge in incoming
                if normalize_edge_condition(edge.condition) == "always"
            ]
            branch_edges = [
                edge
                for edge in incoming
                if normalize_edge_condition(edge.condition) != "always"
            ]
            if always_edges and not branch_edges:
                return (any(edge.from_node not in inactive_node_ids for edge in always_edges)
                        and all(pred_ok(edge, require_success=True) for edge in always_edges))
            if branch_edges and not always_edges:
                return any(pred_ok(edge, require_success=False) for edge in branch_edges)
            return (
                any(pred_ok(edge, require_success=False) for edge in branch_edges)
                or (bool(always_edges) and any(edge.from_node not in inactive_node_ids for edge in always_edges)
                    and all(pred_ok(edge, require_success=True) for edge in always_edges))
            )

        try:
            finalizer = flow.node_map[flow.finalizer_node_id]
            paused_branch_ids: set[str] = set()
            halted_node: ExpertNodeSpec | None = None
            halted_result: ExpertRoleResult | None = None
            inline_recovery_node_ids: set[str] = set()
            candidate_executed_ids: set[str] = {
                node_id for node_id, saved in saved_states.items()
                if saved.get("status") == "completed" and saved.get("reason") == "audit_accepted"
            }
            for batch in flow.topological_batches():
                # Explicit branch joins do not wait for a path that was never
                # selected. Failures/HITL still halt; only inactive edges skip.
                for node_id in batch:
                    if node_id in loop_node_ids and node_id not in root_loops:
                        continue
                    if node_id in paused_branch_ids:
                        continue  # An unresolved predecessor has not selected or rejected a branch.
                    incoming_edges = [edge for edge in flow.edges if edge.to_node == node_id]
                    if not incoming_edges or not all(edge.from_node in results or edge.from_node in inactive_node_ids
                                                     for edge in incoming_edges):
                        continue
                    if any(edge.from_node in results and results[edge.from_node].status != 'completed'
                           and edge.from_node not in inactive_node_ids for edge in incoming_edges):
                        continue
                    active_edges = [edge for edge in incoming_edges if any(
                        item.get("from_node") == edge.from_node and item.get("condition") == edge.condition
                        for item in handoffs.get(node_id) or [])]
                    if not active_edges:
                        skipped_ids = root_loops[node_id].node_ids if node_id in root_loops else [node_id]
                        for skipped_id in skipped_ids:
                            inactive_node_ids.add(skipped_id)
                            if skipped_id in candidate_executed_ids:
                                continue
                            skipped = flow.node_map[skipped_id]
                            await self._emit({"type": "expert_node_completed", "run_id": run_id,
                                              "node_id": skipped_id, "role": skipped.role, "label": skipped.label,
                                              "status": "skipped", "reason": "branch_not_taken", "passed": False})
                runnable = [
                    flow.node_map[node_id]
                    for node_id in batch
                    if node_id != flow.finalizer_node_id
                    and node_id not in skipped_schedule_ids
                    and node_id not in inactive_node_ids
                    and node_id not in paused_branch_ids
                    and (node_id not in loop_node_ids or node_id in root_loops)
                    and (node_id in flow.entry_node_ids() or handoffs.get(node_id))
                ]
                runnable = [
                    node for node in runnable
                    if _predecessors_satisfied(flow, node.node_id, results)
                ]
                if not runnable:
                    continue
                # Orchestration stages must advance one-at-a-time. Sibling
                # entries with no edge between them (common extract artifact)
                # previously raced via gather; run in flow declaration order.
                node_order = {
                    node.node_id: index for index, node in enumerate(flow.nodes)
                }
                runnable.sort(key=lambda node: node_order.get(node.node_id, 10**9))
                # Opt-in only: freeze a genuinely read-only registry for each
                # concurrent node. Output declarations do not sandbox Bash.
                readonly_names = {"ReadFile", "Glob", "Grep", "WebSearch", "InspectGitState", "ReadAuditArtifact", "InspectVisualEvidence", "ReadStageResult"}
                def parallel_safe(candidate):
                    return (candidate.kind == "role" and candidate.role in {"manager", "planner", "auditor"}
                        and candidate.node_id not in loop_node_ids
                        and candidate.failure_policy.action == "stop"
                        and (flow.meta.get("node_kinds") or {}).get(candidate.node_id) != "gate"
                        and not candidate.members and not candidate.responsibility.startswith("[GATE]")
                        and not constraint_pipelines.get(candidate.node_id)
                        and ((candidate.role == "auditor" and candidate.tools is None)
                             or (candidate.tools is not None and set(candidate.tools) <= readonly_names)))

                parallel_results = {}
                parallel_nodes = [n for n in runnable if parallel_safe(n)]
                if flow.meta.get("parallel") is True and len(parallel_nodes) > 1:
                    tasks = [asyncio.create_task(execute_node(n, list(handoffs.get(n.node_id) or []),
                        policy_override=replace(policy_for(n), parallel_readonly=True, allow_delegation=False))) for n in parallel_nodes]
                    try:
                        values = await asyncio.gather(*tasks)
                        parallel_results = dict(zip((n.node_id for n in parallel_nodes), values))
                    finally:
                        for pending_task in tasks:
                            if not pending_task.done():
                                pending_task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                for n in runnable:
                    if n.node_id in parallel_results:
                        results[n.node_id] = parallel_results[n.node_id]
                for node in runnable:
                    if node.node_id in skipped_schedule_ids:
                        continue
                    if node.node_id in root_loops:
                        from mewcode.experts.loop_runtime import run_bounded_loop

                        async def run_loop_node(loop_node, loop_incoming, execution_policy, prompt):
                            return await execute_node(loop_node, loop_incoming, policy_override=execution_policy,
                                                      extra_prompt=prompt)

                        async def emit_loop_event(event):
                            await self._emit({"run_id": run_id, **event})

                        loop = root_loops[node.node_id]
                        local, result = await run_bounded_loop(
                            contract=loop, flow=flow, saved=loop_states, run_node=run_loop_node,
                            emit=emit_loop_event, policy_for=policy_for,
                            incoming=list(handoffs.get(node.node_id) or []),
                            control_policy=checkpoint.get("loop_control_policy"),
                            recover_node=self.recovery_handler,
                        )
                        results.update(local)
                        check_node = flow.node_map[loop.check_node_id]
                        failure_node = flow.node_map.get(result.failed_node_id, check_node)
                        results[failure_node.node_id if result.status != "completed" else loop.check_node_id] = result
                        inline_recovery_node_ids.update(loop.node_ids)
                        if result.status != "completed":
                            halted_node, halted_result = failure_node, result
                            await self._emit({"type": "expert_flow_state", "run_id": run_id,
                                              "phase": "batch_halted", "node_id": failure_node.node_id,
                                              "status": result.status, "reason": result.error_kind or "loop_watchdog"})
                            break
                        await publish_handoffs(check_node, result)
                        continue
                    saved_audit = saved_states.get(node.node_id) or {}
                    saved_protocol = (checkpoint.get("audit_protocols") or {}).get(node.node_id) or {}
                    accepted_checkpoint = (
                        saved_audit.get("status") == "completed"
                        and (saved_audit.get("verdict") or {}).get("verdict") == "PASS"
                        and (not saved_protocol or saved_protocol.get("completed") is True)
                    )
                    quantified = (
                        self.work_dir is not None and node.role == "auditor"
                        and node.failure_policy.action == "route"
                        and bool(node.failure_policy.target_node_id or node.failure_policy.fallback_node_ids)
                        and not accepted_checkpoint
                    )
                    if quantified:
                        from mewcode.experts.audit_protocol import content_hash
                        from mewcode.experts.audit_recovery import run_quantified_audit

                        async def run_review_node(review_node, prompt, execution_policy):
                            candidate_executed_ids.add(review_node.node_id)
                            return await execute_node(
                                review_node, list(handoffs.get(node.node_id) or []),
                                policy_override=execution_policy, extra_prompt=prompt,
                            )

                        async def emit_review_event(event):
                            await self._emit({"run_id": run_id, **event})

                        inline_recovery_node_ids.add(node.node_id)
                        result = await run_quantified_audit(
                            node=node, flow=flow, source=self.work_dir,
                            archive=self.archive_root / "audit-candidates" / run_id / content_hash(node.node_id)[:16],
                            saved=saved_protocol,
                            run_node=run_review_node, emit=emit_review_event,
                            policy=policy_for(node),
                            max_iterations=recovery_executor_max_iterations(),
                            timeout_seconds=recovery_executor_timeout_seconds(), recovery_handler=self.recovery_handler,
                        )
                    elif node.node_id in parallel_results:
                        result = parallel_results[node.node_id]
                    else:
                        result = await execute_node(
                            node, list(handoffs.get(node.node_id) or [])
                        )
                    if result.status == "expanding" and result.node_expansion:
                        result = await execute_runtime_expansion(
                            node,
                            result,
                            list(handoffs.get(node.node_id) or []),
                        )
                    results[node.node_id] = result
                    if result.error_kind == "branch_wait" or result.status == "paused":
                        from mewcode.expert_runs import successors_after_node
                        paused_branch_ids.update({node.node_id, *successors_after_node(flow.as_dict(), node.node_id)})
                        await self._emit({"type": "expert_flow_state", "run_id": run_id, "phase": "branch_paused",
                            "node_id": node.node_id, "status": "paused", "reason": (
                                "awaiting_business_answer" if result.error_kind == 'branch_wait' else result.error_kind or 'node_paused')})
                        continue
                    from mewcode.experts.recovery_state import HARD_KINDS
                    routed_recovery = (
                        not is_success(node, result)
                        and result.status != 'incomplete_audit'
                        and result.error_kind not in HARD_KINDS
                        and (result.status != 'blocked' or result.error_kind in {'capability_missing', 'environment_fault', 'provider_error'})
                        and node.failure_policy.action in {"route", "fallback"}
                        and bool(node.failure_policy.target_node_id or node.failure_policy.fallback_node_ids)
                    )
                    if routed_recovery and not quantified:
                        inline_recovery_node_ids.add(node.node_id)
                        result = await recover_routed_node(node, result)
                        results[node.node_id] = result
                    # A failed routed recovery must not leak its ordinary fail
                    # edge into the main DAG.  Successful recovery publishes
                    # the audit's pass edge and normal scheduling resumes.
                    if is_success(node, result) or not routed_recovery:
                        await publish_handoffs(node, result)
                    if not is_success(node, result):
                        branched = (
                            not routed_recovery
                            and
                            result.status == "completed"
                            and _has_fail_branch(node.node_id)
                        )
                        if not branched:
                            # Declaration-order serial: a blocked/failed stage must
                            # not let later same-batch siblings start (HITL, failed).
                            await self._emit({
                                "type": "expert_flow_state",
                                "run_id": run_id,
                                "phase": "batch_halted",
                                "node_id": node.node_id,
                                "status": result.status,
                                "reason": "stage_not_successful",
                            })
                            halted_node = node
                            halted_result = result
                            break
                if halted_node is not None:
                    break

            final_inputs = [
                {
                    "from_node": edge.from_node,
                    "to_node": flow.finalizer_node_id,
                    "label": flow.node_map[edge.from_node].label,
                    "text": results[edge.from_node].text,
                    "handoff": results[edge.from_node].handoff,
                    "status": results[edge.from_node].status,
                }
                for edge in flow.edges
                if edge.to_node == flow.finalizer_node_id
                and edge.from_node in results
                and edge.from_node not in inactive_node_ids
            ]
            if not final_inputs:
                final_inputs = [
                    {
                        "from_node": node_id,
                        "to_node": flow.finalizer_node_id,
                        "label": flow.node_map[node_id].label,
                        "text": result.text,
                        "handoff": result.handoff,
                        "status": result.status,
                    }
                    for node_id, result in results.items()
            ]
            # A failed / HITL stage already stopped the graph. Stay on that
            # node instead of jumping to the finalizer skip (which names a
            # later unused stage and looks like the run skipped ahead).
            if (
                halted_node is not None
                and halted_result is not None
                and not is_success(
                    halted_node,
                    results.get(halted_node.node_id, halted_result),
                )
            ):
                stuck = results.get(halted_node.node_id, halted_result)
                final_result = ExpertRoleResult(
                    text=(
                        f"{stuck.text.rstrip()}\n"
                        f"已停在「{halted_node.label}」({halted_node.node_id})；"
                        + ("模型连接中断，尚未判定业务验收失败。继续任务会接续该节点，保留本轮已完成节点和已消耗预算；不会整图重跑。"
                           if stuck.error_kind == "provider_interrupted" else
                           recovery_action(stuck.error_kind)
                           if halted_node.node_id in loop_node_ids else
                           "本轮审查已结束但未通过，请明确重试该审查节点；上游成果和历史尝试记忆保留。"
                           if halted_node.role == "auditor" and stuck.status == "completed" else
                           "处理该阶段后点「继续任务」可从这里接力，不必整图重跑。")
                    ),
                    input_tokens=stuck.input_tokens,
                    output_tokens=stuck.output_tokens,
                    status=stuck.status,
                    topology=list(stuck.topology),
                    verdict=stuck.verdict,
                    restored=stuck.restored,
                )
            # Do not run the finalizer while required predecessors are blocked
            # (e.g. a predecessor was never scheduled). Leave the run blocked
            # so the UI can "继续任务" after the graph can advance.
            elif paused_branch_ids:
                paused = [(key, value) for key, value in results.items()
                          if value.status == "paused" or value.error_kind == "branch_wait"]
                details = [f"「{flow.node_map[key].label}」：{value.text}" for key, value in paused]
                stopped = paused[0][1] if paused else None
                final_result = ExpertRoleResult(
                    text="部分阶段已暂停，检查点已保留。" + ("\n" + "\n".join(details) if details else ""),
                    status="paused", error_kind=stopped.error_kind if stopped else "branch_wait",
                    failed_node_id=(stopped.failed_node_id or paused[0][0]) if stopped else "")
            elif not _predecessors_satisfied(flow, flow.finalizer_node_id, results):
                pred_ids = [
                    edge.from_node
                    for edge in flow.edges
                    if edge.to_node == flow.finalizer_node_id
                ]
                details: list[str] = []
                unsuccessful: list[dict[str, str]] = []
                for pred_id in pred_ids:
                    pred_node = flow.node_map.get(pred_id)
                    label = pred_node.label if pred_node is not None else pred_id
                    pred_result = results.get(pred_id)
                    if pred_result is None:
                        status_label = "未执行"
                        details.append(f"- {label} ({pred_id}): {status_label}")
                        unsuccessful.append(
                            {"node_id": pred_id, "label": label, "status": status_label}
                        )
                    elif pred_node is None or not is_success(pred_node, pred_result):
                        status_label = pred_result.status
                        details.append(f"- {label} ({pred_id}): {status_label}")
                        unsuccessful.append(
                            {
                                "node_id": pred_id,
                                "label": label,
                                "status": status_label,
                            }
                        )
                detail_text = "\n".join(details) if details else "- （无前驱明细）"
                final_result = ExpertRoleResult(
                    text=(
                        "Finalizer skipped: required predecessors are not successful\n"
                        "未成功前驱：\n"
                        f"{detail_text}\n"
                        "已成功阶段已写入检查点；处理阻塞后点「继续任务」可从下一未完成阶段接力，"
                        "不必整图重跑。"
                    ),
                    status="needs_attention",
                )
                await self._emit({
                    "type": "expert_flow_state",
                    "run_id": run_id,
                    "phase": "finalizer_skipped",
                    "node_id": finalizer.node_id,
                    "reason": "predecessors_not_successful",
                    "unsuccessful_predecessors": unsuccessful,
                })
            else:
                finalizer_plugin = finalizers.get(flow.finalizer_plugin)
                if callable(finalizer_plugin):
                    node_attempts[finalizer.node_id] = (
                        node_attempts.get(finalizer.node_id, 0) + 1
                    )
                    finalizer_started = time.time()
                    await self._emit({
                        "type": "expert_node_started",
                        "run_id": run_id,
                        "node_id": finalizer.node_id,
                        "role": finalizer.role,
                        "label": finalizer.label,
                        "status": "running",
                        "started_at": finalizer_started,
                        "node_attempt": node_attempts[finalizer.node_id],
                        "provider": "plugin",
                        "model": flow.finalizer_plugin,
                    })
                    await self._emit({
                        "type": "expert_stage",
                        "run_id": run_id,
                        "node_id": finalizer.node_id,
                        "role": finalizer.role,
                        "label": finalizer.label,
                        "status": "running",
                        "started_at": finalizer_started,
                        "provider": "plugin",
                        "model": flow.finalizer_plugin,
                        "attempt": attempt,
                    })
                    try:
                        final_value = finalizer_plugin(
                            flow=flow,
                            node=finalizer,
                            inputs=tuple(dict(item) for item in final_inputs),
                            results=dict(results),
                        )
                        if inspect.isawaitable(final_value):
                            final_value = await final_value
                        if isinstance(final_value, ExpertRoleResult):
                            final_result = final_value
                        elif isinstance(final_value, dict):
                            final_result = ExpertRoleResult(
                                text=str(final_value.get("text") or ""),
                                status=str(final_value.get("status") or "completed"),
                            )
                        else:
                            final_result = ExpertRoleResult(text=str(final_value or ""))
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        final_result = ExpertRoleResult(text=format_error(exc), status="failed")
                    finalizer_completed = time.time()
                    finalizer_record = {
                        "node_id": finalizer.node_id,
                        "role": finalizer.role,
                        "label": finalizer.label,
                        "provider": "plugin",
                        "model": flow.finalizer_plugin,
                        "status": final_result.status,
                        "input_tokens": final_result.input_tokens,
                        "output_tokens": final_result.output_tokens,
                        "started_at": finalizer_started,
                        "completed_at": finalizer_completed,
                        "duration_ms": round(
                            (finalizer_completed - finalizer_started) * 1000
                        ),
                        "node_attempt": node_attempts[finalizer.node_id],
                        "transcript": final_result.text,
                    }
                    records.append(finalizer_record)
                    await self._emit({
                        "type": "expert_node_completed",
                        "run_id": run_id,
                        **{
                            key: value for key, value in finalizer_record.items()
                            if key != "transcript"
                        },
                        "passed": is_success(finalizer, final_result),
                        "finalizer": flow.finalizer_plugin,
                    })
                    await self._emit({
                        "type": "expert_stage",
                        "run_id": run_id,
                        **{
                            key: value for key, value in finalizer_record.items()
                            if key != "transcript"
                        },
                        "attempt": attempt,
                    })
                elif flow.finalizer_plugin == "model":
                    final_result = await execute_node(finalizer, final_inputs)
                else:
                    raise RuntimeError(
                        f"finalizer plugin is unavailable: {flow.finalizer_plugin}"
                    )
            results[finalizer.node_id] = final_result
            from mewcode.experts.outcome import discharged_findings, acceptance_retry_required
            outcome_states = {
                key: {"status": value.status, "verdict": value.verdict, "audit_snapshot": value.audit_snapshot}
                for key, value in results.items()
            }
            # Inactive branches have durable skip events, not runnable results.
            # Include that fact in acceptance as well: an unselected reviewer
            # must not keep an otherwise discharged findings stage unresolved.
            outcome_states.update({key: {"status": "skipped"} for key in inactive_node_ids
                                   if key not in outcome_states})
            discharged = discharged_findings(flow.as_dict(), outcome_states)
            all_required_passed = all(
                is_success(flow.node_map[node_id], result)
                or node_id in discharged
                or (
                    flow.node_map[node_id].role == "auditor"
                    and (flow.meta.get("node_kinds") or {}).get(node_id) == "decision"
                    and result.status == "completed"
                    and _has_fail_branch(node_id)
                )
                for node_id, result in results.items()
                if node_id != flow.finalizer_node_id and node_id not in inactive_node_ids
            )
            finalizer_blocked = (
                not _predecessors_satisfied(flow, flow.finalizer_node_id, results)
            )
            if paused_branch_ids:
                final_status = "paused"
            elif finalizer_blocked:
                # Resumable: completed stages stay in the checkpoint.
                final_status = "blocked"
            elif all_required_passed and is_success(finalizer, final_result):
                final_status = "completed"
            else:
                final_status = "needs_attention"
            transcript_path = self._archive(
                run_id, task, strategy, final_status, records
            )
            summaries = {node_id: result.text for node_id, result in results.items()}
            result = ExpertRunResult(
                run_id=run_id,
                status=final_status,
                strategy=strategy,
                manager_summary=summaries.get("manager.intake", ""),
                planner_summary=summaries.get("planner", ""),
                executor_summary=summaries.get("executor", ""),
                audit_summary=summaries.get("auditor", ""),
                final_summary=final_result.text,
                node_summaries=summaries,
                transcript_path=transcript_path,
            )
            await self._emit({
                "type": "expert_flow_state",
                "run_id": run_id,
                "phase": "done",
                "status": final_status,
                "finalizer_node_id": flow.finalizer_node_id,
                "discharged_audit_node_ids": sorted(discharged),
                "resume_requires_retry": acceptance_retry_required({"status": final_status,
                    "locked_flow": flow.as_dict(), "node_states": outcome_states}),
            })
            if self.lesson_store is not None:
                try:
                    if lesson_read_errors:
                        await self._emit({"type": "expert_lessons_unavailable", "run_id": run_id,
                                          "reason": lesson_read_errors[0]})
                    if final_status == "completed":
                        self.lesson_store.record_success([item for group in injected_lessons.values() for item in group])
                    needs_reflection = final_status != "paused" and (final_status in {"needs_attention", "blocked"}
                        or any(row.get("recovery_round", 0) > 0 for row in records))
                    if self.reflection_runner is not None and needs_reflection:
                        compact_records = [{key: row.get(key) for key in (
                            "node_id", "status", "duration_ms", "fallback_reason", "recovery_round")}
                            | {"open_items": ((row.get("handoff") or {}).get("open_items") or [])[:8]}
                            for row in records[-40:]]
                        proposals = await asyncio.wait_for(self.reflection_runner(compact_records), timeout=60)
                        proposed = []
                        for item in proposals[:5] if isinstance(proposals, list) else []:
                            if not isinstance(item, dict):
                                continue
                            if not str(item.get("text") or "").strip():
                                continue
                            scope = str(item.get("scope") or "*")
                            if scope != "*" and scope not in flow.node_map and scope not in {"role:" + n.role for n in flow.nodes}:
                                continue
                            proposed.append(self.lesson_store.add(flow.flow_id, scope, str(item.get("text") or "")[:2000],
                                source="reflection", run_id=run_id, why=str(item.get("why") or "")))
                        if proposed:
                            await self._emit({"type": "expert_lessons_proposed", "run_id": run_id,
                                "flow_id": flow.flow_id, "count": len(proposed), "lessons": proposed})
                except Exception as exc:
                    await self._emit({"type": "expert_lessons_unavailable", "run_id": run_id,
                                      "reason": str(exc)[:300]})
            await self._emit({
                "type": "expert_run_done",
                "run_id": run_id,
                "status": final_status,
                "strategy": strategy,
                "attempt": attempt,
                "transcript_path": transcript_path,
                "finalizer_node_id": flow.finalizer_node_id,
            })
            return result
        except asyncio.CancelledError:
            await self._cancel_direct_agents()
            transcript_path = self._archive(run_id, task, strategy, "paused", records)
            await self._emit({
                "type": "expert_run_done",
                "run_id": run_id,
                "status": "paused",
                "strategy": strategy,
                "attempt": attempt,
                "transcript_path": transcript_path,
            })
            raise
        except Exception:
            await self._cancel_direct_agents()
            transcript_path = self._archive(run_id, task, strategy, "paused", records)
            await self._emit({
                "type": "expert_run_done",
                "run_id": run_id,
                "status": "paused",
                "strategy": strategy,
                "attempt": attempt,
                "transcript_path": transcript_path,
            })
            raise
    async def _cancel_direct_agents(self) -> None:
        if self.cancel_callback is None:
            return
        try:
            result = self.cancel_callback()
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass

    def _archive(
        self,
        run_id: str,
        task: str,
        strategy: str,
        status: str,
        records: list[dict[str, Any]],
    ) -> str:
        self.archive_root.mkdir(parents=True, exist_ok=True)
        path = self.archive_root / f"{run_id}.json"
        path.write_text(
            json.dumps({
                "run_id": run_id, "task": task, "strategy": strategy,
                "status": status, "stages": records,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(path)
