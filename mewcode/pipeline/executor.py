"""Pipeline 执行引擎。

负责按拓扑序执行 Graph 中的节点，支持：
- task 节点：调用 Agent
- goal 节点：验收判断
- condition 节点：条件分支
- tool 节点：直接调用工具
- skill 节点：调用 Skill
- loop 节点：循环执行
- parallel 节点：并发执行
"""

from __future__ import annotations

import inspect
import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Mapping

from mewcode.pipeline.auto_policy import (
    AUX_FORMAT_FINISH,
    AUX_GOAL_CHECK,
    AUX_PLAN_SPLIT,
    AutoPolicy,
    LlmJudge,
    ModelDecision,
    SOURCE_NODE,
    converge_effort,
    pick_tier_model,
)
from mewcode.pipeline.actions import (
    PipelineAction,
    PipelineActionRequest,
    normalize_actions,
)
from mewcode.pipeline.compiler import (
    CompileError,
    ExecutionPlan,
    KNOWN_EFFORT_LEVELS,
    PipelineCompiler,
)
from mewcode.pipeline.models import (
    CompareOp,
    ConditionMode,
    ExecMode,
    GraphNode,
    LoopMode,
    MODEL_TIER_LIGHT,
    NodeStatus,
    NodeType,
    PipelineGraph,
    PipelineRunRecord,
    RunStatus,
    is_auto,
)
from mewcode.pipeline.variables import VariableContext
from mewcode.errors import NetworkCircuitOpenError, format_error, is_transient_error

log = logging.getLogger(__name__)

# 循环回边的最大迭代次数（防止无限循环）
MAX_LOOP_ITERATIONS = 10

# 任务子步骤/调用的瞬时错误重试次数（可由 config pipeline.granule_retries 覆盖）
DEFAULT_GRANULE_RETRIES = 2

# 连续网络失败熔断阈值（可由 config pipeline.network_breaker_threshold 覆盖）。
# 计数跨节点/子步骤累积，任何一次成功调用即清零；达到阈值后
# 直接熔断，把 run 标记为 interrupted（可断点续跑）而非继续烧重试。
DEFAULT_NETWORK_BREAKER_THRESHOLD = 6

# 叶子节点（task/goal/condition/tool/skill）的默认墙钟超时（秒）。
# 可被节点上的 timeout_seconds 覆盖。loop/parallel 容器节点不受此限制。
DEFAULT_NODE_TIMEOUT = 3600

# LLM prompt 中单个变量允许内联的最大字符数。完整值仍保留在 run_record；
# 超限时仅注入短预览与落盘路径，避免链式节点重复支付大段上游输出。
DEFAULT_MAX_INLINE_OUTPUT_CHARS = 8_000
INTERPOLATION_PREVIEW_CHARS = 500


class PipelineError(Exception):
    """Pipeline 执行错误基类。"""


class NodeTimeoutError(PipelineError):
    """节点执行超过墙钟时限。"""


class LoopExhaustedError(PipelineError):
    """条件驱动的重试回边达到迭代上限仍未通过验收。"""


class BudgetExceededError(PipelineError):
    """token 预算耗尽。"""


class OutputSchemaError(PipelineError):
    """节点定义了 output_schema 但结果无法解析为合法 JSON。"""


class ParallelBranchError(PipelineError):
    """parallel 容器的某个分支节点执行失败（携带分支节点 id 定位）。"""

    def __init__(self, branch_node_id: str, message: str) -> None:
        self.branch_node_id = branch_node_id
        super().__init__(
            f"Parallel branch failed at node '{branch_node_id}': {message}"
        )


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 格式字符串。"""
    return datetime.now(timezone.utc).isoformat()


class PipelineExecutor:
    """Pipeline Graph 执行器。"""

    def __init__(
        self,
        *,
        agent_factory: Callable[..., Coroutine[Any, Any, tuple[str, dict | None]]] | None = None,
        tool_executor: Callable[[str, dict], Coroutine[Any, Any, str]] | None = None,
        skill_executor: Callable[[str, dict], Coroutine[Any, Any, str]] | None = None,
        action_provider: Callable[[], Mapping[str, PipelineAction]] | None = None,
        capabilities_provider: Callable[[], Coroutine[Any, Any, dict]] | None = None,
        config: Any | None = None,
        auto_policy: AutoPolicy | None = None,
        asset_resolver: Callable[[Any], dict[str, Any]] | None = None,
        artifact_writer: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None = None,
        artifact_dir: str | Path | None = None,
        on_node_start: Callable[[str, str], None] | None = None,
        on_node_complete: Callable[[str, str, str], None] | None = None,
        on_node_error: Callable[[str, str], None] | None = None,
        on_log: Callable[[str], None] | None = None,
        on_checkpoint: Callable[[PipelineRunRecord], None] | None = None,
    ) -> None:
        """初始化执行器。

        Args:
            agent_factory: Agent 调用工厂函数 (prompt, schema, model, effort, isolation) -> (result, usage)
            tool_executor: 工具执行函数 (tool_name, params) -> result
            skill_executor: Skill 执行函数 (skill_name, args) -> result
            action_provider: 动态 Pipeline Action 目录；每次 run 开始读取一次，
                使插件安装/卸载无需重启 Pipeline API。
            capabilities_provider: 能力枚举提供者（与 API 层同源，异步返回
                {"models": [{"id", "efforts"}, ...], ...}）。每次 execute()
                开头拉取一次，供 auto_policy 的三级回退决策使用；
                缺失/失败时降级为空列表（走 config/内置分档表兜底）
            config: AppConfig 或 PipelineConfig（pipeline 自动挡档位配置）
            auto_policy: 自动挡策略实例；None 时每个 executor 自建一个
                （executor 每 run 一个实例，细判缓存随之自然回收）
            on_node_start: 节点开始回调 (node_id, node_type)
            on_node_complete: 节点完成回调 (node_id, node_type, result_summary)
            on_node_error: 节点错误回调 (node_id, error_message)
            on_log: 日志回调 (message)
            on_checkpoint: 增量持久化回调 (run_record)——每个节点完成/失败后调用，
                用于把运行进度落盘，支持崩溃恢复与断点续跑
        """
        self._agent_factory = agent_factory
        self._tool_executor = tool_executor
        self._skill_executor = skill_executor
        self._action_provider = action_provider
        self._capabilities_provider = capabilities_provider
        self._config = config
        self._policy = auto_policy or AutoPolicy()
        self._asset_resolver = asset_resolver
        self._artifact_writer = artifact_writer
        self._artifact_dir = (
            Path(artifact_dir).resolve() if artifact_dir is not None else None
        )
        self._persisted_interpolations: dict[tuple[str, str, str], Path] = {}
        self._on_node_start = on_node_start
        self._on_node_complete = on_node_complete
        self._on_node_error = on_node_error
        self._on_log = on_log
        self._on_checkpoint = on_checkpoint
        # 通用事件发射器（供 SSE 实时推送）：接收 dict 事件，可同步或异步
        self.on_event: Callable[[dict], Any] | None = None
        # 每次 execute() 的运行态（executor 应当每 run 一个实例）
        self.current_run_record: PipelineRunRecord | None = None
        self._budget_total: int | None = None
        self._resume_seeded: set[str] = set()
        # 当前正在执行的节点 ID（供变量插值失败的 warning 事件定位；
        # 在 _dispatch_node 的同步段内赋值，并发下归因仍准确，见该处注释）
        self._current_node_id: str | None = None
        # 事件流序号（每 run 从 1 递增；_emit 统一附加）
        self._event_seq = 0
        # 网络熔断器：连续瞬时失败计数（跨节点/子步骤累积，成功即清零）
        self._net_fail_streak = 0
        # 能力上下文（execute() 开头从 capabilities_provider 拉取）
        self._available_models: list[Any] = []
        self._session_model: str | None = None
        # 各节点本次执行的 model/effort 决策（node_start 前解析）
        self._node_decisions: dict[str, ModelDecision | None] = {}
        # 指定 schedule 为入口代表“定时已触发”，允许穿透到右侧；
        # 从普通上游抵达 schedule 只布防，不会立即执行下游。
        self._entry_node_id: str | None = None
        self._node_attachments: dict[str, list[dict[str, Any]]] = {}
        self._node_requires_vision: dict[str, bool] = {}
        self._unattended = False
        self._actions: dict[str, PipelineAction] = {}
        self._used_actions: set[str] = set()

    def _emit(self, event: dict) -> None:
        """发射一个事件：统一附加递增 seq 与 ts（事件流持久化契约）。"""
        self._event_seq += 1
        event = {"seq": self._event_seq, "ts": _now_iso(), **event}
        if self.on_event is None:
            return
        try:
            result = self.on_event(event)
            # 支持异步回调（fire-and-forget）
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
        except Exception as e:
            log.debug("on_event callback failed: %s", e)

    def _on_missing_variable(self, ref: str) -> None:
        """变量插值失败回调：发 warning 事件并指明引用名。"""
        self._log(f"Variable '{{{{{ref}}}}}' unresolved, replaced with empty string")
        self._emit({
            "type": "warning",
            "node_id": self._current_node_id,
            "ref": ref,
            "message": f"变量 '{{{{{ref}}}}}' 无法解析，已替换为空串",
        })

    async def execute(
        self,
        graph: PipelineGraph,
        trigger_input: dict[str, Any] | None = None,
        *,
        budget_total: int | None = None,
        run_id: str | None = None,
        resume_from: PipelineRunRecord | None = None,
        entry_node_id: str | None = None,
    ) -> PipelineRunRecord:
        """执行 Pipeline。

        Args:
            graph: Pipeline 图定义。
            trigger_input: 触发器输入。
            budget_total: token 预算上限（超过后拒绝发起新的 agent 调用）。
            run_id: 预分配的 run_id（API 层先返回 run_id、后台执行时使用）。
            resume_from: 断点续跑的来源记录——已完成节点的结果被复用，
                失败/未执行的节点重新执行。
            entry_node_id: 入口节点 ID。None = 从 start 整图执行；指定时
                只执行该节点及其下游可达子图（定时触发 / 从节点运行），
                子图外的上游输出引用按缺失变量处理。

        Returns:
            PipelineRunRecord 执行记录。
        """
        # 创建执行记录
        run_record = PipelineRunRecord(
            graph_id=graph.graph_id,
            graph_name=graph.name,
            status=RunStatus.RUNNING,
            trigger_input=trigger_input or {},
            budget_total=budget_total,
            started_at=_now_iso(),
            entry_node_id=entry_node_id,
        )
        if run_id:
            run_record.run_id = run_id
        self.current_run_record = run_record
        self._budget_total = budget_total
        self._resume_seeded = set()
        self._event_seq = 0
        self._node_decisions = {}
        self._entry_node_id = entry_node_id
        self._persisted_interpolations = {}

        # 能力上下文：可用模型（含各自 effort 档位）与会话当前模型，
        # 供 auto_policy 三级回退决策；拉取失败降级为空
        self._node_attachments = {}
        self._node_requires_vision = {}
        self._unattended = bool((trigger_input or {}).get("trigger", {}).get("type") == "schedule")
        self._load_actions()
        self._used_actions = set()
        await self._load_capabilities()

        # 断点续跑：注入上一次已完成节点的记录与输出
        if resume_from is not None:
            run_record.resumed_from = resume_from.run_id
            run_record.context_outputs = dict(resume_from.context_outputs)
            run_record.total_input_tokens = resume_from.total_input_tokens
            run_record.total_output_tokens = resume_from.total_output_tokens
            run_record.total_cache_read_tokens = resume_from.total_cache_read_tokens
            run_record.total_cache_creation_tokens = resume_from.total_cache_creation_tokens
            run_record.cache_usage_available = resume_from.cache_usage_available
            for nid, rec in resume_from.node_records.items():
                if rec.status == NodeStatus.COMPLETED:
                    run_record.node_records[nid] = rec.model_copy(deep=True)
                    self._resume_seeded.add(nid)
            self._log(
                f"Resuming from run {resume_from.run_id}: "
                f"{len(self._resume_seeded)} completed node(s) reused"
            )

        self._log(f"Starting pipeline '{graph.name}' ({graph.graph_id})")
        self._emit({
            "type": "run_start", "graph_id": graph.graph_id, "name": graph.name,
            "run_id": run_record.run_id, "resumed_from": run_record.resumed_from,
            "entry_node_id": entry_node_id,
            "source": "schedule" if self._unattended else "manual",
        })
        self._checkpoint(run_record)

        try:
            # 入口节点存在性前置校验（入 try：失败按正常 FAILED 流程落盘）
            if entry_node_id is not None and graph.get_node(entry_node_id) is None:
                raise CompileError(
                    f"Entry node '{entry_node_id}' not found in graph"
                )

            # 编译图
            plan = PipelineCompiler.compile(graph)

            # 初始化变量上下文；插值失败（引用无法解析）时发 warning 事件
            # 而非把 {{...}} 原样拼进 prompt
            ctx = VariableContext(
                trigger_input, on_missing=self._on_missing_variable
            )

            # 断点续跑：恢复上游输出，供变量插值和条件判断使用
            for key, value in run_record.context_outputs.items():
                ctx.set_output(key, value)

            # 应用全局变量
            for key, value in graph.variables.items():
                interpolated = ctx.interpolate(value)
                ctx.set_custom(key, interpolated)

            # 工作列表遍历执行（支持分支与有界循环）
            await self._run_worklist(
                graph, plan, ctx, run_record, entry_node_id=entry_node_id
            )

            # 完成
            run_record.status = RunStatus.COMPLETED
            run_record.completed_at = _now_iso()
            self._log(f"Pipeline '{graph.name}' completed successfully")
            self._emit({"type": "run_complete", "status": "completed", "run_id": run_record.run_id})

        except asyncio.CancelledError:
            # 取消（服务停止 / 用户主动取消）→ 标记 INTERRUPTED 并落盘，
            # 之后可通过 resume 从断点续跑。重新抛出以遵守 asyncio 取消协议。
            run_record.status = RunStatus.INTERRUPTED
            run_record.completed_at = _now_iso()
            if not run_record.error_message:
                run_record.error_message = "Run interrupted (cancelled)"
            self._log(f"Pipeline '{graph.name}' interrupted")
            self._emit({"type": "run_complete", "status": "interrupted", "run_id": run_record.run_id})
            self._checkpoint(run_record)
            raise

        except NetworkCircuitOpenError as e:
            # 网络熔断 → 可恢复的中断（非失败）：已完成节点的成果保留，
            # 网络恢复后断点续跑，不浪费已烧掉的 token。
            run_record.status = RunStatus.INTERRUPTED
            run_record.completed_at = _now_iso()
            run_record.error_message = format_error(e)
            self._log(f"Pipeline '{graph.name}' interrupted by network circuit breaker")
            self._emit({
                "type": "run_complete", "status": "interrupted",
                "error": format_error(e), "run_id": run_record.run_id,
            })

        except BudgetExceededError as e:
            # 预算耗尽是可恢复的中断：不要把已完成节点与已记账用量
            # 误报为失败，允许调用方调整预算后从断点续跑。
            run_record.status = RunStatus.INTERRUPTED
            run_record.completed_at = _now_iso()
            run_record.error_message = format_error(e)
            self._log(f"Pipeline '{graph.name}' interrupted by token budget")
            self._emit({
                "type": "run_complete", "status": "interrupted",
                "error": format_error(e), "run_id": run_record.run_id,
            })

        except Exception as e:
            run_record.status = RunStatus.FAILED
            run_record.completed_at = _now_iso()
            run_record.error_message = format_error(e)
            self._log(f"Pipeline '{graph.name}' failed: {format_error(e)}")
            self._emit({
                "type": "run_complete", "status": "failed",
                "error": format_error(e), "run_id": run_record.run_id,
            })
            log.exception("Pipeline execution failed")

        finally:
            # 细判缓存清理（契约：每次 run 结束调用，防长驻进程缓存泄漏）
            self._policy.clear_judge_cache(run_record.run_id)
            await self._cleanup_actions(run_record.run_id)

        self._checkpoint(run_record)
        return run_record

    def _load_actions(self) -> None:
        self._actions = {}
        if self._action_provider is None:
            return
        try:
            self._actions = normalize_actions(self._action_provider())
        except Exception as exc:
            log.warning("pipeline action provider failed: %s", exc)

    async def _cleanup_actions(self, run_id: str) -> None:
        """Release run-scoped plugin state without changing the run outcome."""

        for action_id in sorted(self._used_actions):
            action = self._actions.get(action_id)
            if action is None:
                continue
            try:
                await action.close_run(run_id)
            except Exception as exc:
                log.warning(
                    "pipeline action %s cleanup failed for %s: %s",
                    action_id,
                    run_id,
                    exc,
                )

    async def _load_capabilities(self) -> None:
        """从 capabilities_provider 拉取可用模型列表与会话当前模型。

        契约：models 每项为 {"id": str, "efforts": [str, ...]}，当前模型
        排首位（作为 session_model 兜底）。provider 缺失/异常/无模型数据
        时降级为空列表，auto_policy 走 config 档位配置 / 内置分档表兜底。
        """
        self._available_models = []
        self._session_model = None
        if self._capabilities_provider is None:
            return
        try:
            data = await self._capabilities_provider()
        except Exception as e:
            log.debug("capabilities provider failed: %s", e)
            return
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            return
        self._available_models = models
        for item in models:
            mid = item.get("id") if isinstance(item, dict) else item
            if mid:
                self._session_model = str(mid)
                break

    # ------------------------------------------------------------------
    def _model_supports_vision(self, model: str | None) -> bool:
        target = (model or "").strip().lower()
        return any(
            isinstance(item, dict)
            and str(item.get("id", "")).strip().lower() == target
            and bool(item.get("vision", False))
            for item in self._available_models
        )

    def _vision_models(self) -> list[Any]:
        return [
            item for item in self._available_models
            if isinstance(item, dict) and bool(item.get("vision", False))
        ]

    @staticmethod
    def _task_requires_vision(node: GraphNode, ctx: VariableContext) -> bool:
        return bool(
            node.attachments
            or ctx.extract_asset_refs(node.goal, node.prompt, node.boundary)
            or ctx.collect_asset_refs(ctx.get("input.attachments"))
        )

    def _resolve_node_attachments(self, node: GraphNode, ctx: VariableContext) -> list[dict[str, Any]]:
        refs: list[Any] = [ref.model_dump() for ref in node.attachments]
        refs.extend(ctx.extract_asset_refs(node.goal, node.prompt, node.boundary))
        refs.extend(ctx.collect_asset_refs(ctx.get("input.attachments")))
        unique = {str(ref.get("asset")): ref for ref in refs if isinstance(ref, dict)}
        if unique and self._asset_resolver is None:
            raise PipelineError("pipeline image assets cannot be resolved")
        return [self._asset_resolver(ref) for ref in unique.values()] if self._asset_resolver else []


    # model/effort 三级回退决策
    # ------------------------------------------------------------------

    async def _resolve_decision(
        self,
        node: GraphNode,
        graph: PipelineGraph,
        run_record: PipelineRunRecord,
    ) -> ModelDecision | None:
        """按节点类型解析本次执行的 model/effort 决策。

        - task：完整三级回退 + 自动挡（规则层秒判 / LLM 细判）；
        - condition(judgment)：三级回退（规则层恒判轻档，无需细判）；
        - goal：辅助调用强制轻档+low（节点显式 model/effort 仍优先）；
        - 其余节点类型不发起模型调用 → None（node_start 不带 resolved 字段）。
        """
        action = self._actions.get(node.action_id) if node.type == NodeType.ACTION else None
        if node.type == NodeType.TASK or (action is not None and action.uses_model):
            policy_node = node
            if (
                node.type == NodeType.ACTION
                and not node.goal
                and not node.prompt
                and node.params.get("prompt")
            ):
                policy_node = node.model_copy(
                    update={"goal": str(node.params.get("prompt"))}
                )
            e_node, e_graph, notes = self._sanitize_overrides(policy_node, graph)
            available_models = self._available_models
            session_model = self._session_model
            if self._node_requires_vision.get(node.id, False):
                available_models = self._vision_models()
                if not available_models:
                    raise PipelineError("task includes images but no vision-capable model is available")
                if not self._model_supports_vision(session_model):
                    session_model = str(available_models[0].get("id"))
            decision = await self._policy.resolve(
                e_node, e_graph, self._config, available_models,
                self._make_llm_judge(node, run_record),
                session_model=session_model,
                run_id=run_record.run_id,
            )
            if self._node_requires_vision.get(node.id, False) and not self._model_supports_vision(decision.model):
                raise PipelineError(f"model '{decision.model}' does not support image input")
            return self._with_notes(decision, notes)
        if (
            node.type == NodeType.CONDITION
            and node.condition_mode == ConditionMode.JUDGMENT
        ):
            e_node, e_graph, notes = self._sanitize_overrides(node, graph)
            decision = await self._policy.resolve(
                e_node, e_graph, self._config, self._available_models, None,
                session_model=self._session_model,
                run_id=run_record.run_id,
            )
            return self._with_notes(decision, notes)
        if node.type == NodeType.GOAL:
            return self._resolve_goal_check(node, graph)
        return None

    def _resolve_goal_check(
        self, node: GraphNode, graph: PipelineGraph
    ) -> ModelDecision:
        """goal 核验的决策：辅助调用强制轻档+low，节点显式覆盖优先。"""
        e_node, _, notes = self._sanitize_overrides(node, graph)
        aux = self._policy.resolve_aux(
            AUX_GOAL_CHECK, self._config, self._available_models,
            session_model=self._session_model,
        )
        if is_auto(e_node.model) and is_auto(e_node.effort):
            return self._with_notes(aux, notes)

        model, effort, source = aux.model, aux.effort, aux.source
        parts = list(notes)
        if not is_auto(e_node.model):
            model = e_node.model.strip()
            source = SOURCE_NODE
            parts.append(f"goal 核验模型由节点显式指定 '{model}'")
        if not is_auto(e_node.effort):
            cand = e_node.effort.strip().lower()
            effort = converge_effort(cand, model, self._available_models)
            parts.append(f"effort 由节点显式指定 '{cand}'")
        elif not is_auto(e_node.model):
            # 显式模型 + 自动 effort：轻档默认档位按新模型收敛
            effort = converge_effort(aux.effort, model, self._available_models)
        parts.append(aux.reason)
        return ModelDecision(
            model=model, effort=effort, reason="；".join(parts), source=source
        )

    def _sanitize_overrides(
        self, node: GraphNode, graph: PipelineGraph | None
    ) -> tuple[GraphNode, PipelineGraph | None, list[str]]:
        """运行时兜底：非法/未知的显式 model/effort 视同 auto 回退（不报错）。

        保存期的 warning 由 compiler 负责；这里只做回退并记录理由：
        - 显式模型不在可用模型列表内（列表非空时才判定）→ 清为 auto；
        - 显式 effort 不在 KNOWN_EFFORT_LEVELS 全集内 → 清为 auto；
        - 图默认 default_model/default_effort 同样处理。

        Returns:
            (节点副本, 图副本, 回退理由列表)；无需回退时返回原对象。
        """
        notes: list[str] = []
        known_ids: set[str] = set()
        for item in self._available_models:
            mid = item.get("id") if isinstance(item, dict) else item
            if mid:
                known_ids.add(str(mid).strip().lower())

        def check_model(value: str | None, owner: str) -> tuple[str | None, bool]:
            if is_auto(value):
                return value, False
            if known_ids and value.strip().lower() not in known_ids:
                notes.append(
                    f"{owner}模型 '{value}' 不在可用模型列表，视同 auto 回退"
                )
                return None, True
            return value, False

        def check_effort(value: str | None, owner: str) -> tuple[str | None, bool]:
            if is_auto(value):
                return value, False
            if value.strip().lower() not in KNOWN_EFFORT_LEVELS:
                notes.append(
                    f"{owner}effort '{value}' 非法（已知档位: "
                    f"{', '.join(KNOWN_EFFORT_LEVELS)}），视同 auto 回退"
                )
                return None, True
            return value, False

        n_model, ch1 = check_model(node.model, "节点")
        n_effort, ch2 = check_effort(node.effort, "节点")
        e_node = (
            node.model_copy(update={"model": n_model, "effort": n_effort})
            if (ch1 or ch2) else node
        )

        e_graph = graph
        if graph is not None:
            g_model, ch3 = check_model(graph.default_model, "图默认")
            g_effort, ch4 = check_effort(graph.default_effort, "图默认")
            if ch3 or ch4:
                e_graph = graph.model_copy(
                    update={"default_model": g_model, "default_effort": g_effort}
                )
        return e_node, e_graph, notes

    @staticmethod
    def _with_notes(
        decision: ModelDecision, notes: list[str]
    ) -> ModelDecision:
        """把回退理由前置进决策 reason（无理由时原样返回）。"""
        if not notes:
            return decision
        return ModelDecision(
            model=decision.model,
            effort=decision.effort,
            reason="；".join(notes + [decision.reason]),
            source=decision.source,
        )

    def _make_llm_judge(
        self, node: GraphNode, run_record: PipelineRunRecord
    ) -> LlmJudge | None:
        """构造自动挡 LLM 细判调用：轻档模型 + low 的最小 agent 调用。

        auto_policy 保证同一 (run_id, node_id) 至多细判一次；失败时由
        auto_policy 自行降级规则默认轻档。细判用量计入节点，并发
        aux_call(kind=tier_judge) 事件供 events.jsonl 观测。
        """
        if self._agent_factory is None:
            return None

        async def judge(judge_prompt: str) -> str:
            self._check_budget(run_record)
            model, pick_reason = pick_tier_model(
                MODEL_TIER_LIGHT, self._config, self._available_models,
                session_model=self._session_model,
            )
            effort = converge_effort("low", model, self._available_models)
            self._emit({
                "type": "aux_call",
                "kind": "tier_judge",
                "node_id": node.id,
                "model": model,
                "effort": effort,
                "reason": f"自动挡 LLM 细判（{pick_reason}）",
            })
            result, usage = await self._agent_factory(
                prompt=judge_prompt,
                schema=None,
                model=model,
                effort=effort,
                isolation=None,
                on_event=None,
            )
            self._record_usage(run_record, node.id, usage)
            return str(result)

        return judge

    def _resolve_aux(self, kind: str, node_id: str) -> ModelDecision:
        """解析一次辅助调用（强制轻档+low）并发 aux_call 事件。"""
        aux = self._policy.resolve_aux(
            kind, self._config, self._available_models,
            session_model=self._session_model,
        )
        self._emit({
            "type": "aux_call",
            "kind": kind,
            "node_id": node_id,
            "model": aux.model,
            "effort": aux.effort,
            "reason": aux.reason,
        })
        return aux

    def _checkpoint(self, run_record: PipelineRunRecord) -> None:
        """增量持久化运行进度（尽力而为，失败不影响执行）。"""
        if self._on_checkpoint is None:
            return
        try:
            self._on_checkpoint(run_record)
        except Exception as e:
            log.debug("checkpoint failed: %s", e)

    async def _run_worklist(
        self,
        graph: PipelineGraph,
        plan: ExecutionPlan,
        ctx: VariableContext,
        run_record: PipelineRunRecord,
        *,
        entry_node_id: str | None = None,
    ) -> None:
        """就绪并发的工作列表调度（join 语义）。

        - join：节点的全部有效前驱（前向入边，不含回边）完成后才就绪，
          菱形汇聚点（A→B、A→C、B→D、C→D 中的 D）恰好执行一次。
        - 并发：同一波就绪的节点用 asyncio.gather 并发执行。
        - 条件分支剪枝（死路径消除）：未命中的分支从其目标的等待计数中
          扣除；目标的全部入边都被剪掉时该节点不执行，并把剪枝继续向
          下游传播——否则汇聚点会一直等一个永远不会完成的前驱。
        - 回边（循环）不计入等待计数：回边命中时目标直接重新入队，
          带迭代上限；循环体下游的计数进入负数区后，只有「真正走到」
          的完成信号会再次触发执行（剪枝信号不会）。
        - 容器作用域（loop 体 / parallel 分支，编译期算好）：体内节点
          由容器内部执行，外层调度完全跳过；容器完成/死亡后沿其
          「边界出边」（作用域内 → 作用域外）推进外层下游。
        - 入口参数化（entry_node_id）：种子从 start 换成指定节点后，
          可达性剪枝自然把入口上游（含 start 及其他支路）排除在执行外，
          下游子图仍按 join/分支/循环语义从前往后执行。
        - 定时关卡：普通流程从左侧到达 schedule 时只布防并停止该支路；
          只有 entry_node_id 指向该 schedule（调度器到点触发）才放行出边。
        """
        # 出边表：node_id -> [edge]
        out_edges: dict[str, list] = {n.id: [] for n in graph.nodes}
        for edge in graph.edges:
            out_edges.setdefault(edge.from_node, []).append(edge)

        # 容器作用域：成员做传递闭包（嵌套容器的体也算外层容器的成员），
        # 边界出边 = 从成员集合指向集合外的边
        scope_owned = set(plan.scope_owner)
        containers: set[str] = set(plan.loop_scopes) | set(plan.parallel_scopes)
        members: dict[str, set[str]] = {cid: {cid} for cid in containers}
        for nid, owner_id in plan.scope_owner.items():
            o: str | None = owner_id
            guard: set[str] = set()
            while o is not None and o not in guard:
                guard.add(o)
                if o in members:
                    members[o].add(nid)
                o = plan.scope_owner.get(o)
        boundary: dict[str, list] = {
            cid: [
                e for e in graph.edges
                if e.from_node in mem and e.to_node not in mem
            ]
            for cid, mem in members.items()
        }

        def node_out_edges(nid: str) -> list:
            """节点完成/死亡后要推进的出边：容器用边界出边。"""
            return boundary[nid] if nid in boundary else out_edges.get(nid, [])

        start: GraphNode | None = None
        if entry_node_id is not None:
            start = plan.node_map.get(entry_node_id)
        if start is None:
            start = graph.get_start_node()
        if start is None:
            # 没有 start 节点时，退化为拓扑序执行（容器体内节点跳过）
            for node in plan.topo_order:
                if node.id in scope_owned:
                    continue
                await self._execute_node(node, graph, plan, ctx, run_record)
            return

        # 从入口可达的节点集合（沿全部出边，含回边）
        reachable: set[str] = set()
        stack = [start.id]
        while stack:
            nid = stack.pop()
            if nid in reachable:
                continue
            reachable.add(nid)
            for edge in out_edges.get(nid, []):
                stack.append(edge.to_node)

        # 等待计数：编译期算好的有效入度，再扣掉来源不可达的前向边
        # （不可达前驱永远不会执行，计入会让下游永远不就绪）
        remaining: dict[str, int] = dict(plan.in_degree)
        for edge in graph.edges:
            if edge.id in plan.back_edges:
                continue
            if edge.from_node not in reachable and edge.to_node in remaining:
                remaining[edge.to_node] -= 1

        # fired：节点是否至少有一条入边被「真正走到」（区别于剪枝扣除）
        fired: dict[str, bool] = {n.id: False for n in graph.nodes}
        fired[start.id] = True

        pruned_edges: set[str] = set()
        loop_count: dict[str, int] = {}

        def resolve_edge(
            edge: Any, taken: bool, batch: list[str], in_batch: set[str]
        ) -> None:
            """一条前向入边被解决（走到或剪掉），推进目标的等待计数。

            调度不变量：
            - remaining[n] 单调递减；首次降到 0 时 n 的全部有效入边已
              解决：有任一入边走到（fired）→ 就绪；全部被剪掉 → 死节点，
              剪枝沿其前向出边继续传播。
            - 负数区（循环重入后）：只有 taken 信号再次触发就绪，剪枝
              信号不触发——否则循环里反复未走的旧分支会误唤醒下游。
            - 每条边的剪枝只扣一次（pruned_edges 去重），防止循环中反复
              剪同一条边把兄弟前驱的名额扣光、把汇聚点提前唤醒或错杀。
            - 容器体内节点（scope_owned）不参与外层计数/就绪，指向它们
              的边直接忽略；容器死亡时沿边界出边传播剪枝。
            """
            target = edge.to_node
            if target in scope_owned or target not in remaining:
                return
            if not taken:
                if edge.id in pruned_edges:
                    return
                pruned_edges.add(edge.id)
            remaining[target] -= 1
            if taken:
                fired[target] = True
            left = remaining[target]
            if left == 0 and not fired[target]:
                # 全部入边都被剪掉 → 死节点，向下游传播剪枝
                # （容器死亡时沿边界出边传播，体内节点无需单独剪）
                for out in node_out_edges(target):
                    if out.id not in plan.back_edges:
                        resolve_edge(out, False, batch, in_batch)
                return
            if (left == 0 and fired[target]) or (left < 0 and taken):
                if target not in in_batch:
                    batch.append(target)
                    in_batch.add(target)

        batch: list[str] = [start.id]
        while batch:
            nodes = [plan.node_map[nid] for nid in batch if nid in plan.node_map]
            next_batch: list[str] = []
            in_next: set[str] = set()

            # 就绪节点并发执行（单节点直接 await，免任务开销）
            if len(nodes) == 1:
                results: list[Any] = [
                    await self._execute_node(nodes[0], graph, plan, ctx, run_record)
                ]
            else:
                results = list(await asyncio.gather(
                    *(
                        self._execute_node(n, graph, plan, ctx, run_record)
                        for n in nodes
                    ),
                    return_exceptions=True,
                ))
                # 任一节点失败 → 整条 run 失败；return_exceptions 让并行
                # 兄弟节点先跑完（记录/事件完整）再抛出第一个错误
                first_err = next(
                    (r for r in results if isinstance(r, BaseException)), None
                )
                if first_err is not None:
                    raise first_err

            # 处理完成信号，推进后继（容器节点沿边界出边推进）
            for node, result in zip(nodes, results):
                if (
                    node.type == NodeType.SCHEDULE
                    and entry_node_id != node.id
                ):
                    self._emit({
                        "type": "schedule_armed",
                        "node_id": node.id,
                    })
                    continue

                # 条件节点：只跟随命中的分支
                taken_label = None
                if node.type == NodeType.CONDITION and isinstance(result, dict):
                    taken_label = result.get("branch")

                for edge in node_out_edges(node.id):
                    followed = not (
                        taken_label is not None
                        and edge.label
                        and edge.label != taken_label
                    )
                    if followed:
                        self._emit({
                            "type": "edge_taken",
                            "edge_id": edge.id,
                            "from": edge.from_node,
                            "to": edge.to_node,
                            "label": edge.label,
                            "node_id": edge.from_node,
                        })
                    if edge.id not in plan.back_edges:
                        resolve_edge(edge, followed, next_batch, in_next)
                        continue

                    # 回边 → 循环（不参与等待计数）
                    if not followed:
                        continue
                    cnt = loop_count.get(edge.id, 0) + 1
                    loop_count[edge.id] = cnt
                    if cnt > MAX_LOOP_ITERATIONS:
                        self._log(
                            f"Loop edge '{edge.id}' exceeded "
                            f"{MAX_LOOP_ITERATIONS} iterations, stopping loop"
                        )
                        self._emit({
                            "type": "loop_limit",
                            "node_id": node.id,
                            "iterations": MAX_LOOP_ITERATIONS,
                        })
                        if node.type == NodeType.CONDITION:
                            # 条件驱动的重试回边耗尽 = 验收始终未通过。
                            # 静默继续会把一条从未达标的 run 标成「成功」，
                            # 必须显式失败。
                            raise LoopExhaustedError(
                                f"Retry loop exhausted: condition "
                                f"'{node.get_display_name()}' still not passing "
                                f"after {MAX_LOOP_ITERATIONS} iterations"
                            )
                        continue
                    self._log(f"Loop back to '{edge.to_node}' (iteration {cnt})")
                    self._emit({
                        "type": "loop_iter",
                        "node_id": node.id,
                        "target": edge.to_node,
                        "iteration": cnt,
                    })
                    if edge.to_node not in in_next and edge.to_node not in scope_owned:
                        next_batch.append(edge.to_node)
                        in_next.add(edge.to_node)

            batch = next_batch

    # 应用墙钟超时的叶子节点类型（容器节点 loop/parallel 不设超时，
    # 其内部的叶子节点各自受限）
    _LEAF_TYPES = (
        NodeType.TASK, NodeType.GOAL, NodeType.CONDITION,
        NodeType.TOOL, NodeType.SKILL, NodeType.ACTION,
    )

    async def _execute_node(
        self,
        node: GraphNode,
        graph: PipelineGraph,
        plan: ExecutionPlan,
        ctx: VariableContext,
        run_record: PipelineRunRecord,
    ) -> Any:
        """执行单个节点。"""
        # 断点续跑：本 run 中首次遇到已在上次运行完成的节点 → 直接复用结果。
        # 只复用一次（discard）：回边循环里第二次到达时正常重新执行。
        if node.id in self._resume_seeded:
            self._resume_seeded.discard(node.id)
            key = node.output_key or node.id
            if key in run_record.context_outputs:
                cached = run_record.context_outputs[key]
                ctx.set_output(key, cached)
                summary = self._smart_truncate(str(cached), 500)
                self._log(f"Node '{node.get_display_name()}': reusing result from previous run")
                self._emit({
                    "type": "node_start", "node_id": node.id,
                    "node_type": node.type.value,
                    "title": node.get_display_name(), "cached": True,
                })
                self._emit({
                    "type": "node_output", "node_id": node.id,
                    "title": node.get_display_name(),
                    "output": summary, "cached": True,
                })
                self._emit({
                    "type": "node_done", "node_id": node.id,
                    "status": "completed", "cached": True,
                })
                if self._on_node_complete:
                    self._on_node_complete(node.id, node.type.value, summary)
                return cached
            # 没有可复用的输出（start/end 等）→ 正常执行

        # 三级回退解析本次执行的 model/effort（节点显式 → 图默认 → 自动挡），
        # 决策随 node_start 事件上报（resolved_model/resolved_effort/reason）
        self._node_requires_vision[node.id] = self._task_requires_vision(node, ctx)
        decision: ModelDecision | None = None
        try:
            decision = await self._resolve_decision(node, graph, run_record)
        except Exception as e:
            if self._node_requires_vision.get(node.id, False):
                raise
            # 决策失败不阻断执行：回退 agent_factory 默认模型
            log.warning(
                "model/effort resolution failed for node '%s': %s", node.id, e
            )
        self._node_decisions[node.id] = decision

        # 通知开始
        if self._on_node_start:
            self._on_node_start(node.id, node.type.value)
        start_event: dict[str, Any] = {
            "type": "node_start",
            "node_id": node.id,
            "node_type": node.type.value,
            "title": node.get_display_name(),
        }
        if decision is not None:
            start_event.update(decision.as_event_fields())
        self._emit(start_event)

        run_record.update_node_status(
            node.id,
            NodeStatus.RUNNING,
            node_type=node.type,
            started_at=_now_iso(),
        )

        try:
            if node.type in self._LEAF_TYPES:
                timeout_s = node.timeout_seconds or DEFAULT_NODE_TIMEOUT
                result = await self._run_with_timeout(
                    self._dispatch_node(node, graph, plan, ctx, run_record),
                    timeout_s,
                    node,
                )
            else:
                result = await self._dispatch_node(node, graph, plan, ctx, run_record)

            # 保存输出（完整值进 ctx 与 run_record，供下游与断点续跑使用）
            key = node.output_key or node.id
            ctx.set_output(key, result)
            run_record.context_outputs[key] = result

            # 更新记录（摘要采用头尾截断：交付物/结论通常在末尾）
            result_summary = self._smart_truncate(str(result), 500) if result else ""
            run_record.update_node_status(
                node.id,
                NodeStatus.COMPLETED,
                completed_at=_now_iso(),
                result_summary=result_summary,
            )
            self._checkpoint(run_record)

            # 通知完成
            if self._on_node_complete:
                self._on_node_complete(node.id, node.type.value, result_summary)
            self._emit({
                "type": "node_output",
                "node_id": node.id,
                "title": node.get_display_name(),
                "output": result_summary,
            })
            self._emit({"type": "node_done", "node_id": node.id, "status": "completed"})

            return result

        except asyncio.CancelledError:
            run_record.update_node_status(
                node.id,
                NodeStatus.FAILED,
                completed_at=_now_iso(),
                error_message="Interrupted (cancelled)",
            )
            self._checkpoint(run_record)
            raise

        except Exception as e:
            err = format_error(e)
            run_record.update_node_status(
                node.id,
                NodeStatus.FAILED,
                completed_at=_now_iso(),
                error_message=err,
            )
            self._checkpoint(run_record)

            if self._on_node_error:
                self._on_node_error(node.id, err)
            self._emit({"type": "node_done", "node_id": node.id, "status": "failed", "error": err})

            raise

    @staticmethod
    async def _run_with_timeout(coro: Any, timeout_s: float, node: GraphNode) -> Any:
        """带墙钟超时地执行节点协程。

        不能用 asyncio.wait_for 的 except TimeoutError 判断超时——
        节点内部（LLM 客户端）抛出的 TimeoutError 会与墙钟到期混淆，
        导致「2 分钟的 API 超时」被误报成「节点超过 3600s」。
        这里用 asyncio.wait 区分：只有任务真的没跑完才算节点超时，
        内部异常原样透传。
        """
        task = asyncio.ensure_future(coro)
        try:
            done, pending = await asyncio.wait({task}, timeout=timeout_s)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            raise NodeTimeoutError(
                f"Node '{node.get_display_name()}' exceeded time limit ({timeout_s}s)"
            )
        return task.result()

    async def _dispatch_node(
        self,
        node: GraphNode,
        graph: PipelineGraph,
        plan: ExecutionPlan,
        ctx: VariableContext,
        run_record: PipelineRunRecord,
    ) -> Any:
        """按节点类型分发执行。"""
        # 在节点自身的同步段内记录当前节点 ID，供变量插值失败的 warning
        # 事件归因。ctx.interpolate 是同步调用，且各 _exec_* 的插值都发生
        # 在其首个 await 之前，与本赋值处于同一同步段；并发波内其他节点
        # 只能在 await 点插入执行，不会窜改归因。
        self._current_node_id = node.id
        if node.type == NodeType.START:
            return await self._exec_start(node, ctx)
        elif node.type == NodeType.SCHEDULE:
            return await self._exec_schedule(node, ctx)
        elif node.type == NodeType.END:
            return await self._exec_end(node, ctx)
        elif node.type == NodeType.TASK:
            return await self._exec_task(node, ctx, run_record)
        elif node.type == NodeType.GOAL:
            return await self._exec_goal(node, ctx, run_record)
        elif node.type == NodeType.CONDITION:
            return await self._exec_condition(node, graph, plan, ctx, run_record)
        elif node.type == NodeType.TOOL:
            return await self._exec_tool(node, ctx)
        elif node.type == NodeType.SKILL:
            return await self._exec_skill(node, ctx, run_record)
        elif node.type == NodeType.ACTION:
            return await self._exec_action(node, ctx, run_record)
        elif node.type == NodeType.LOOP:
            return await self._exec_loop(node, graph, plan, ctx, run_record)
        elif node.type == NodeType.PARALLEL:
            return await self._exec_parallel(node, graph, plan, ctx, run_record)
        return None

    # ------------------------------------------------------------------
    # 节点执行实现
    # ------------------------------------------------------------------

    async def _exec_start(self, node: GraphNode, ctx: VariableContext) -> dict:
        """执行 start 节点。"""
        self._log(f"Start: {node.label or 'Begin'}")
        return {"status": "started"}

    async def _exec_schedule(self, node: GraphNode, ctx: VariableContext) -> dict:
        """执行 schedule 关卡：上游到达仅布防，定时入口才放行。"""
        triggered = self._entry_node_id == node.id
        status = "triggered" if triggered else "armed"
        self._log(f"Schedule {status}: {node.get_display_name()}")
        result = {
            "status": status,
            "schedule_text": node.schedule.schedule_text if node.schedule else "",
        }
        if triggered:
            result["fired_at"] = (
                ctx.get("input.trigger.fired_at") or _now_iso()
            )
        else:
            result["armed_at"] = _now_iso()
        return result

    async def _exec_end(self, node: GraphNode, ctx: VariableContext) -> dict:
        """执行 end 节点。"""
        self._log(f"End: {node.label or 'Complete'}")
        return {"status": "completed"}

    async def _exec_task(
        self, node: GraphNode, ctx: VariableContext, run_record: PipelineRunRecord
    ) -> Any:
        """执行 task 节点（Agent 调用）。

        按人类下发任务的方式组装 prompt：
        目标 + 详情 + 模式约束 + 工具/Skill 提示 + 输出结构要求。
        如果定义了 output_schema，结果必须能解析为 dict（先直接解析，
        失败则追加一次「格式化收尾」调用），否则节点失败——
        输出契约是下游指标核验和断点续跑的基础，不允许静默降级为文本。
        """
        if self._agent_factory is None:
            raise RuntimeError("agent_factory is not set")

        prompt = self._compose_task_prompt(node, ctx)
        self._node_attachments[node.id] = self._resolve_node_attachments(node, ctx)
        scheduled_trigger = ctx.get("input.trigger.type") == "schedule"
        self._log(f"Task '{node.title or node.goal[:20]}' [{node.mode.value}]: executing")
        self._emit({
            "type": "node_prompt",
            "node_id": node.id,
            "mode": node.mode.value,
            "prompt": prompt,
            "tools": node.tools,
            "skill": node.skill or "",
        })

        # 包装事件回调，将 factory 内部的思考/工具事件归属到当前节点
        def _task_event(ev: dict) -> None:
            ev = dict(ev)
            content_blocks = ev.pop("content_blocks", [])
            artifacts: list[dict[str, Any]] = []
            if self._artifact_writer is not None:
                for block in content_blocks:
                    if isinstance(block, dict) and block.get("type") == "image":
                        artifact = self._artifact_writer(
                            run_record.run_id, node.id, block
                        )
                        artifacts.append(artifact)
                        self._emit({
                            "type": "artifact", "node_id": node.id,
                            "artifact": artifact,
                        })
            if artifacts:
                ev["artifacts"] = artifacts
            ev.setdefault("node_id", node.id)
            self._emit(ev)

        # 本次执行的 model/effort 决策（_execute_node 已解析并随 node_start 上报）
        decision = self._node_decisions.get(node.id)

        # 内部粒子化：大任务拆为子步骤逐步执行（对外仍是一个节点）
        # 定时副作用必须由同一个 Agent 保留工具调用上下文并给出凭据。
        # 拆成“执行”和“汇报”两个无共享会话的粒子会让收尾只能转述，
        # 既无法判断实际返回值，也容易在后续粒子中重复副作用。
        if node.auto_split and not scheduled_trigger:
            result = await self._exec_task_granular(
                node, prompt, _task_event, run_record, decision
            )
        else:
            result, usage = await self._call_with_retry(
                node, prompt, _task_event, run_record, decision=decision
            )
            self._record_usage(run_record, node.id, usage)

        result_str = str(result)

        # 有输出结构定义 → 必须解析出 JSON dict
        if node.output_schema:
            parsed = self._try_parse_json(result_str)
            if parsed is None:
                parsed = await self._finish_structured_output(
                    node, result_str, _task_event, run_record
                )
            if parsed is None:
                raise OutputSchemaError(
                    f"Task '{node.get_display_name()}' defines output_schema but "
                    f"the result could not be parsed as JSON"
                )
            return parsed

        return result_str

    async def _finish_structured_output(
        self,
        node: GraphNode,
        result_str: str,
        on_event: Callable[[dict], None],
        run_record: PipelineRunRecord,
    ) -> dict | None:
        """结构化收尾：让模型把执行结果转成 output_schema 要求的 JSON。

        辅助调用强制轻档+low（resolve_aux），不使用节点自身的模型配置。
        """
        fields_desc = "\n".join(f"  - {k}: {v}" for k, v in node.output_schema.items())
        finish_prompt = (
            "以下是一个任务的执行过程输出（可能被截断）：\n\n"
            + self._smart_truncate(result_str, 6000)
            + "\n\n请根据上述内容，只输出一个 JSON 对象（不要输出任何其他文字），字段如下：\n"
            + fields_desc
        )
        try:
            aux = self._resolve_aux(AUX_FORMAT_FINISH, node.id)
            finish_result, usage = await self._call_with_retry(
                node, finish_prompt, on_event, run_record,
                max_retries=1, decision=aux,
            )
        except NetworkCircuitOpenError:
            raise  # 熔断必须透传到 execute() 标记 interrupted，不可降级吞掉
        except Exception as e:
            self._log(f"Task '{node.id}': structured-output finishing call failed: {format_error(e)}")
            return None
        self._record_usage(run_record, node.id, usage)
        return self._try_parse_json(str(finish_result))

    def _record_usage(
        self, run_record: PipelineRunRecord, node_id: str, usage: dict | None
    ) -> None:
        """把一次 agent 调用的 token 用量累加到节点与 run 总计，并发事件。"""
        if not usage:
            return
        ti = int(usage.get("input_tokens", 0) or 0)
        to = int(usage.get("output_tokens", 0) or 0)
        cr = int(usage.get("cache_read", 0) or 0)
        cc = int(usage.get("cache_creation", 0) or 0)
        cache_usage_available = (
            bool(usage.get("cache_usage_available", False)) or cr > 0 or cc > 0
        )
        if not ti and not to and not cr and not cc and not cache_usage_available:
            return
        rec = run_record.node_records.get(node_id)
        if rec is not None:
            rec.input_tokens += ti
            rec.output_tokens += to
            rec.cache_read_tokens += cr
            rec.cache_creation_tokens += cc
            rec.cache_usage_available = (
                rec.cache_usage_available or cache_usage_available
            )
        run_record.total_input_tokens += ti
        run_record.total_output_tokens += to
        run_record.total_cache_read_tokens += cr
        run_record.total_cache_creation_tokens += cc
        run_record.cache_usage_available = (
            run_record.cache_usage_available or cache_usage_available
        )
        self._emit({
            "type": "node_usage",
            "node_id": node_id,
            "input_tokens": rec.input_tokens if rec else ti,
            "output_tokens": rec.output_tokens if rec else to,
            "cache_read": rec.cache_read_tokens if rec else cr,
            "cache_creation": rec.cache_creation_tokens if rec else cc,
            "cache_usage_available": (
                rec.cache_usage_available if rec else cache_usage_available
            ),
        })

    def _check_budget(self, run_record: PipelineRunRecord) -> None:
        """预算检查：已用量达到上限后拒绝发起新的 agent 调用。"""
        if not self._budget_total:
            return
        used = (
            run_record.total_input_tokens
            + run_record.total_output_tokens
            + run_record.total_cache_read_tokens
            + run_record.total_cache_creation_tokens
        )
        if used >= self._budget_total:
            raise BudgetExceededError(
                f"Token budget exhausted: {used:,} / {self._budget_total:,}"
            )

    # ------------------------------------------------------------------
    # 内部粒子化执行
    # ------------------------------------------------------------------

    async def _exec_task_granular(
        self,
        node: GraphNode,
        base_prompt: str,
        on_event: Callable[[dict], None],
        run_record: PipelineRunRecord,
        decision: ModelDecision | None = None,
    ) -> Any:
        """将任务拆为子步骤逐步执行，每个子步骤可独立重试。

        对外仍表现为一个节点；内部提高长任务的抗超时/抗抖动能力。
        分解失败时退化为单次执行。拆解规划是辅助调用（强制轻档+low），
        子步骤执行沿用节点主决策（decision）。
        """
        # 1. 分解为子步骤（辅助调用：轻档 + low）
        plan_prompt = (
            "你是一个任务规划器。把下面的任务分解为 2-5 个可独立执行的子步骤，"
            "按执行顺序排列，最后一步应产出最终交付物。\n"
            "只输出一个 JSON 数组，每个元素是一句简短的步骤描述，不要输出任何其他文字。\n"
            '示例：["抓取页面数据", "解析并整理", "写入文件并返回路径"]\n\n'
            "任务：\n" + base_prompt
        )
        steps: list[str] = []
        try:
            plan_aux = self._resolve_aux(AUX_PLAN_SPLIT, node.id)
            plan_result, plan_usage = await self._call_with_retry(
                node, plan_prompt, on_event, run_record,
                max_retries=1, decision=plan_aux,
            )
            self._record_usage(run_record, node.id, plan_usage)
            steps = self._parse_steps(str(plan_result))
        except NetworkCircuitOpenError:
            raise  # 熔断必须透传到 execute() 标记 interrupted，不可降级吞掉
        except Exception as e:
            self._log(f"Task '{node.id}': decomposition failed ({format_error(e)}), fallback to single call")

        if not steps:
            self._log(f"Task '{node.id}': no granules, single call")
            result, usage = await self._call_with_retry(
                node, base_prompt, on_event, run_record, decision=decision
            )
            self._record_usage(run_record, node.id, usage)
            return result

        self._log(f"Task '{node.id}': split into {len(steps)} granules")
        self._emit({"type": "granules", "node_id": node.id, "steps": steps})

        # 2. 逐步执行，累积上下文
        done_context: list[str] = []
        last_result = ""
        for i, step in enumerate(steps):
            self._emit({
                "type": "granule_start", "node_id": node.id,
                "index": i, "total": len(steps), "step": step,
            })
            step_prompt = (
                f"## 总目标\n{base_prompt}\n\n"
                f"## 已完成的子步骤\n"
                + ("\n".join(done_context) if done_context else "（暂无）")
                + f"\n\n## 当前要执行的子步骤（第 {i + 1}/{len(steps)} 步）\n{step}\n\n"
                "请执行当前子步骤。完成后简要汇报本步结果（若产出文件请给出路径）。"
            )
            result, usage = await self._call_with_retry(
                node, step_prompt, on_event, run_record, decision=decision
            )
            self._record_usage(run_record, node.id, usage)
            result_str = str(result)
            last_result = result_str
            # 头尾截断：结论/文件路径通常在末尾，不能被 [:800] 切掉
            done_context.append(
                f"[步骤{i + 1} {step}] 结果：{self._smart_truncate(result_str, 800)}"
            )
            self._emit({
                "type": "granule_done", "node_id": node.id,
                "index": i, "total": len(steps),
                "result": self._smart_truncate(result_str, 500),
            })

        return last_result

    def _pipeline_cfg_int(self, key: str, default: int) -> int:
        """从配置读取 pipeline 整型参数；None/缺失/非法时用代码默认。

        兼容 self._config 为 AppConfig（取 .pipeline）或 PipelineConfig 本身。
        """
        cfg = self._config
        if cfg is None:
            return default
        pc = getattr(cfg, "pipeline", cfg)
        val = getattr(pc, key, None)
        return val if isinstance(val, int) and not isinstance(val, bool) else default

    async def _invoke_agent_factory(self, **kwargs: Any) -> tuple[Any, dict | None]:
        """Call rich and legacy factories without breaking existing integrations."""
        if self._agent_factory is None:
            raise RuntimeError("agent_factory is not set")
        filtered = kwargs
        try:
            signature = inspect.signature(self._agent_factory)
            if not any(
                item.kind == inspect.Parameter.VAR_KEYWORD
                for item in signature.parameters.values()
            ):
                filtered = {
                    key: value for key, value in kwargs.items()
                    if key in signature.parameters
                }
        except (TypeError, ValueError):
            pass
        return await self._agent_factory(**filtered)

    async def _call_with_retry(
        self,
        node: GraphNode,
        prompt: str,
        on_event: Callable[[dict], None],
        run_record: PipelineRunRecord,
        max_retries: int | None = None,
        decision: ModelDecision | None = None,
    ) -> tuple[Any, dict | None]:
        """调用 agent_factory，对瞬时错误（超时/网络）自动重试。

        model/effort 取三级回退的解析决策（decision）；未提供决策时回退
        节点原始值（自动挡语义值归一为 None，交由 agent_factory 默认模型，
        绝不把 "auto" 哨兵串传给底层客户端）。

        注意：这是「整段会话」级重试（丢弃已进行的对话、从零重跑），
        仅作为兜底——单次 LLM 请求级的重试在 agent.run 内部完成
        （对话保留、只重发失败的请求），绝大多数瞬时错误在那一层就被吸收。
        """
        if decision is not None:
            model, effort = decision.model, decision.effort
        else:
            model = None if is_auto(node.model) else node.model
            effort = None if is_auto(node.effort) else node.effort
        if max_retries is None:
            retries = self._pipeline_cfg_int("granule_retries", DEFAULT_GRANULE_RETRIES)
        else:
            retries = max_retries
        breaker_threshold = self._pipeline_cfg_int(
            "network_breaker_threshold", DEFAULT_NETWORK_BREAKER_THRESHOLD
        )
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            self._check_budget(run_record)
            try:
                result = await self._invoke_agent_factory(
                    prompt=prompt,
                    schema=None,
                    model=model,
                    effort=effort,
                    isolation=None,
                    on_event=on_event,
                    attachments=self._node_attachments.get(node.id, []),
                    tools=list(node.tools),
                    mode=node.mode.value,
                    boundary=node.boundary,
                    unattended=self._unattended,
                    run_id=run_record.run_id,
                    node_id=node.id,
                )
                # 成功调用 → 熔断计数清零
                self._net_fail_streak = 0
                return result
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_err = e
                if not self._is_transient_error(e):
                    raise

                # 熔断器：连续瞬时失败跨节点累积，达阈即断，
                # 不再继续烧完剩余重试——持续断网时快速止损，
                # 由 execute() 标记 interrupted，网络恢复后断点续跑。
                self._net_fail_streak += 1
                if self._net_fail_streak >= breaker_threshold:
                    self._log(
                        f"Network circuit open: {self._net_fail_streak} consecutive "
                        f"failures (threshold {breaker_threshold}), interrupting run"
                    )
                    self._emit({
                        "type": "circuit_open",
                        "node_id": node.id,
                        "streak": self._net_fail_streak,
                        "threshold": breaker_threshold,
                        "error": format_error(e),
                    })
                    raise NetworkCircuitOpenError(
                        self._net_fail_streak, breaker_threshold, format_error(e)
                    ) from e

                if attempt < retries:
                    wait = float(2 ** attempt)
                    retry_after = getattr(e, "retry_after", None)
                    if retry_after:
                        try:
                            wait = max(wait, float(retry_after))
                        except (TypeError, ValueError):
                            pass
                    self._log(
                        f"Task '{node.id}': transient error ({format_error(e)}), "
                        f"retry {attempt + 1}/{retries} in {wait:.0f}s"
                    )
                    self._emit({
                        "type": "retry", "node_id": node.id,
                        "attempt": attempt + 1, "error": format_error(e),
                    })
                    await asyncio.sleep(wait)
                    continue

                # 主调用（max_retries 未显式限制）重试耗尽且仍是瞬时错误
                # → 网络持续不可用，直接熔断（而非 FAILED）：已完成
                # 节点的成果保留，网络恢复后断点续跑。辅助调用
                # （max_retries=1 的规划/格式化）不在此列——它们有
                # 自己的降级路径，由 streak 累积达阈値时熔断。
                if max_retries is None:
                    self._log(
                        f"Task '{node.id}': retries exhausted on transient error, "
                        f"opening network circuit"
                    )
                    self._emit({
                        "type": "circuit_open",
                        "node_id": node.id,
                        "streak": self._net_fail_streak,
                        "threshold": breaker_threshold,
                        "error": format_error(e),
                    })
                    raise NetworkCircuitOpenError(
                        self._net_fail_streak, breaker_threshold, format_error(e)
                    ) from e
                raise
        assert last_err is not None
        raise last_err

    @staticmethod
    def _serialize_interpolation_value(value: Any) -> str:
        if isinstance(value, (dict, list)):
            try:
                return json.dumps(value, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                pass
        return str(value)

    def _format_prompt_interpolation(self, ref: str, value: Any) -> str:
        """Bound large upstream values before they enter a downstream prompt."""
        text = self._serialize_interpolation_value(value)
        limit = self._pipeline_cfg_int(
            "max_inline_output_chars",
            DEFAULT_MAX_INLINE_OUTPUT_CHARS,
        )
        if len(text) <= limit:
            return text

        preview = self._smart_truncate(text, INTERPOLATION_PREVIEW_CHARS)
        run_record = self.current_run_record
        persisted_path: Path | None = None
        if self._artifact_dir is not None and run_record is not None:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
            key = (run_record.run_id, ref, digest)
            persisted_path = self._persisted_interpolations.get(key)
            if persisted_path is None:
                safe_run = re.sub(
                    r"[^A-Za-z0-9_.-]+",
                    "_",
                    run_record.run_id,
                )[:64] or "run"
                safe_node = re.sub(
                    r"[^A-Za-z0-9_.-]+",
                    "_",
                    self._current_node_id or "node",
                )[:48]
                safe_ref = re.sub(r"[^A-Za-z0-9_.-]+", "_", ref)[:64]
                persisted_path = (
                    self._artifact_dir
                    / safe_run
                    / f"{safe_node}-{safe_ref}-{digest}.txt"
                )
                try:
                    persisted_path.parent.mkdir(parents=True, exist_ok=True)
                    persisted_path.write_text(text, encoding="utf-8")
                    self._persisted_interpolations[key] = persisted_path
                except OSError as exc:
                    log.warning(
                        "Unable to persist large pipeline interpolation '%s': %s",
                        ref,
                        exc,
                    )
                    persisted_path = None

        path_text = str(persisted_path) if persisted_path is not None else "(落盘失败)"
        compacted = (
            f'<persisted-output ref="{ref}" chars="{len(text)}">\n'
            f"上游完整输出已从 prompt 省略以控制 token。\n"
            f"完整内容路径: {path_text}\n"
            f"需要细节时使用 ReadFile 读取该路径。\n"
            f"预览:\n{preview}\n"
            "</persisted-output>"
        )
        self._emit({
            "type": "interpolation_compacted",
            "node_id": self._current_node_id,
            "ref": ref,
            "original_chars": len(text),
            "injected_chars": len(compacted),
            "path": str(persisted_path) if persisted_path is not None else "",
        })
        return compacted

    @staticmethod
    def _is_transient_error(e: Exception) -> bool:
        """判断是否为可重试的瞬时错误。委托给 mewcode.errors 的共享实现。"""
        return is_transient_error(e)

    @staticmethod
    def _smart_truncate(text: str, limit: int, head_ratio: float = 0.4) -> str:
        """头尾截断：保留开头（背景）与结尾（结论/文件路径）。

        朴素的 text[:limit] 会把末尾的交付物切掉——文件路径、最终结论
        几乎总是出现在文本末尾。
        """
        if len(text) <= limit:
            return text
        head = max(int(limit * head_ratio), 0)
        tail = max(limit - head, 0)
        marker = "\n…(中间已截断)…\n"
        return text[:head] + marker + text[-tail:] if tail else text[:head] + marker

    @staticmethod
    def _parse_steps(text: str) -> list[str]:
        """从规划输出中解析子步骤 JSON 数组。"""
        import json as _json
        import re

        try:
            obj = _json.loads(text)
            if isinstance(obj, list):
                return [str(x).strip() for x in obj if str(x).strip()]
        except (ValueError, TypeError):
            pass

        m = re.search(r"```(?:json)?\s*\n?(\[[\s\S]*?\])\s*\n?```", text)
        candidate = m.group(1) if m else None
        if candidate is None:
            s = text.find("[")
            e = text.rfind("]")
            if s >= 0 and e > s:
                candidate = text[s : e + 1]
        if candidate:
            try:
                obj = _json.loads(candidate)
                if isinstance(obj, list):
                    return [str(x).strip() for x in obj if str(x).strip()]
            except (ValueError, TypeError):
                pass
        return []

    @staticmethod
    def _try_parse_json(text: str) -> dict | None:
        """尝试从文本中提取 JSON 对象。"""
        import json as _json
        # 直接解析
        try:
            obj = _json.loads(text)
            if isinstance(obj, dict):
                return obj
        except (ValueError, TypeError):
            pass
        # 提取 ```json ... ``` 代码块
        import re
        m = re.search(r"```(?:json)?\s*\n?(\{[\s\S]*?\})\s*\n?```", text)
        if m:
            try:
                obj = _json.loads(m.group(1))
                if isinstance(obj, dict):
                    return obj
            except (ValueError, TypeError):
                pass
        # 提取第一个 { ... }
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                obj = _json.loads(text[start : end + 1])
                if isinstance(obj, dict):
                    return obj
            except (ValueError, TypeError):
                pass
        return None

    def _compose_task_prompt(self, node: GraphNode, ctx: VariableContext) -> str:
        """根据执行模式组装任务 prompt。"""
        goal = ctx.interpolate(
            node.goal, value_formatter=self._format_prompt_interpolation
        ).strip()
        detail = ctx.interpolate(
            node.prompt, value_formatter=self._format_prompt_interpolation
        ).strip()

        parts: list[str] = []
        if goal:
            parts.append(f"## 任务目标\n{goal}")
        if detail:
            parts.append(f"## 任务详情\n{detail}")

        if ctx.get("input.trigger.type") == "schedule":
            run_index = ctx.get("input.trigger.run_index") or "?"
            max_runs = ctx.get("input.trigger.max_runs") or "?"
            parts.append(
                "## 定时轮次约束\n"
                f"这是定时任务第 {run_index}/{max_runs} 轮。成功的目标动作只能发生一次，"
                "不得为了核实而重复产生副作用。先确认所选命令或 API 在当前环境可用；"
                "若某种方案在产生可见副作用前就失败，可以改用另一种可用方案。外部命令、"
                "系统调用或 API 明确成功后立即返回，不要等待通知、弹窗、声音或临时进程"
                "持续存在。\n"
                "最终只输出一个 JSON 对象："
                '{"status":"succeeded或failed","summary":"实际结果",'
                '"evidence":{"type":"exit_code、process_id或api_result",'
                '"value":"工具返回的实际值"}}。'
                "只有动作真实发生且取得工具返回凭据时才能写 succeeded；"
                "‘已尝试’或‘已启动’本身不算凭据。"
            )

        # 模式约束
        if node.mode == ExecMode.PLAN:
            parts.append(
                "## 执行方式\n"
                "先制定一个分步计划（列出每一步要做什么、预期产出），"
                "然后严格按计划逐步执行，每完成一步汇报进展。"
            )
        elif node.mode == ExecMode.GO:
            boundary = ctx.interpolate(
                node.boundary, value_formatter=self._format_prompt_interpolation
            ).strip()
            parts.append(
                "## 执行方式\n"
                "直接执行，不需要额外确认。但必须严格遵守以下边界，"
                "一旦超出边界立即停止并报告：\n" + boundary
            )
        # react 模式不加额外约束，保持自主循环

        # 工具提示
        if node.tools:
            parts.append(f"## 可用工具\n优先使用：{', '.join(node.tools)}")

        # Skill 提示
        if node.skill:
            parts.append(f"## 指定 Skill\n使用 '{node.skill}' 技能完成此任务。")

        # 输出结构要求
        if node.output_schema:
            fields_desc = "\n".join(
                f"  - {k}: {v}" for k, v in node.output_schema.items()
            )
            parts.append(
                "## 输出要求\n"
                "完成后必须输出一个 JSON 对象（不要包含其他文字），字段如下：\n"
                f"{fields_desc}"
            )

        return "\n\n".join(parts) if parts else goal or detail

    async def _exec_goal(
        self, node: GraphNode, ctx: VariableContext, run_record: PipelineRunRecord
    ) -> dict:
        """执行 goal 节点（验收判断）。

        goal 核验属辅助调用：强制轻档+low（节点显式 model/effort 仍优先），
        决策已在 _execute_node 解析并随 node_start 上报。
        """
        if self._agent_factory is None:
            raise RuntimeError("agent_factory is not set")

        # 构建验收 prompt
        criteria = ctx.interpolate(node.criteria)
        prompt = (
            f"请判断以下内容是否满足验收标准。\n\n"
            f"验收标准：{criteria}\n\n"
            f"回答的第一个词必须是 PASS 或 FAIL（大写），然后另起说明原因。"
        )

        self._log(f"Goal '{node.title}': checking criteria")

        def _goal_event(ev: dict) -> None:
            ev = dict(ev)
            ev.setdefault("node_id", node.id)
            self._emit(ev)

        # 经过 _call_with_retry：验收调用同样受瞬时错误重试保护，
        # 一次网络抖动不再让整条 run 报废
        result, usage = await self._call_with_retry(
            node, prompt, _goal_event, run_record,
            decision=self._node_decisions.get(node.id),
        )
        self._record_usage(run_record, node.id, usage)

        return {
            "passed": self._parse_verdict(str(result)),
            "reason": str(result),
        }

    @staticmethod
    def _parse_verdict(text: str) -> bool:
        """解析 PASS/FAIL 裁决：取文本中第一个独立出现的 PASS 或 FAIL。

        旧实现 `"PASS" in text` 是错的——模型回答
        「FAIL，因为不满足 PASS 所需条件」会被误判为通过。
        两个词都没出现时按 FAIL 处理（fail-closed）。
        """
        import re as _re

        m = _re.search(r"\b(PASS|FAIL)\b", text.upper())
        return bool(m and m.group(1) == "PASS")

    async def _exec_condition(
        self,
        node: GraphNode,
        graph: PipelineGraph,
        plan: ExecutionPlan,
        ctx: VariableContext,
        run_record: PipelineRunRecord,
    ) -> dict:
        """执行 condition 节点（双模核验）。

        - expression 模式：取上游结构化输出的字段做比较，零 token、确定性。
        - judgment 模式：让 Agent 阅读上游输出并按标准判断 PASS/FAIL。
        """
        # 获取上游输出
        upstream = ctx.get(node.ref_task) if node.ref_task else None

        if node.condition_mode == ConditionMode.EXPRESSION:
            result = self._eval_expression(node, upstream)
            self._log(
                f"Condition [expression]: {node.ref_task}.{node.ref_field} "
                f"{node.operator.value} '{node.compare_value}' -> {result}"
            )
        else:
            result = await self._eval_judgment(node, upstream, ctx, run_record)
            self._log(f"Condition [judgment]: '{node.title}' -> {'PASS' if result else 'FAIL'}")

        branch_label = "yes" if result else "no"

        # 计算命中分支的目标节点（实际分支跟随由工作列表根据 edge.label 处理）
        target_node_id = None
        for edge in graph.edges:
            if edge.from_node == node.id and edge.label == branch_label:
                target_node_id = edge.to_node
                break

        return {
            "result": result,
            "branch": branch_label,
            "target": target_node_id,
        }

    def _eval_expression(self, node: GraphNode, upstream: Any) -> bool:
        """指标核验：对上游结构化输出做字段比较。"""
        if not isinstance(upstream, dict):
            self._log(f"Condition [expression]: upstream is not a dict, cannot check field '{node.ref_field}'")
            return False

        value = upstream.get(node.ref_field)
        expected = node.compare_value

        op = node.operator
        try:
            if op == CompareOp.EQ:
                return str(value) == expected
            elif op == CompareOp.NE:
                return str(value) != expected
            elif op == CompareOp.GT:
                return float(value) > float(expected)
            elif op == CompareOp.GE:
                return float(value) >= float(expected)
            elif op == CompareOp.LT:
                return float(value) < float(expected)
            elif op == CompareOp.LE:
                return float(value) <= float(expected)
            elif op == CompareOp.CONTAINS:
                return expected in str(value)
        except (ValueError, TypeError) as e:
            self._log(f"Condition [expression]: comparison failed: {e}")
            return False
        return False

    async def _eval_judgment(
        self, node: GraphNode, upstream: Any, ctx: VariableContext,
        run_record: PipelineRunRecord,
    ) -> bool:
        """LLM 核验：让 Agent 阅读上游输出并按标准判断。"""
        if self._agent_factory is None:
            raise RuntimeError("agent_factory is not set for judgment condition")

        criteria = ctx.interpolate(node.criteria).strip()
        upstream_text = str(upstream) if upstream is not None else "(无上游输出)"
        # 头尾截断：结论/文件路径通常在末尾，只保留开头会把关键证据切掉
        upstream_text = self._smart_truncate(upstream_text, 8000)

        prompt = (
            "你是一个严格的核验员。请阅读以下内容，判断它是否满足核验标准。\n"
            "注意：内容描述了「过程」不代表「完成」——只有明确给出结果/交付物"
            "（如具体文件路径）才算满足。可以核实的事实（文件是否存在等）"
            "如有工具可用应实际核实。\n\n"
            f"## 核验标准\n{criteria}\n\n"
            f"## 待核验内容\n{upstream_text}\n\n"
            "## 要求\n"
            "回答的第一个词必须是 PASS 或 FAIL（大写），然后紧跟一句理由。"
        )

        def _judge_event(ev: dict) -> None:
            ev = dict(ev)
            ev.setdefault("node_id", node.id)
            self._emit(ev)

        # 经过 _call_with_retry：核验调用同样受瞬时错误重试保护。
        # model/effort 决策（三级回退；自动挡 → 轻档+low）已在
        # _execute_node 解析（旧实现的 effort="low" 孤例硬编码收编进 auto_policy）
        result, usage = await self._call_with_retry(
            node, prompt, _judge_event, run_record,
            decision=self._node_decisions.get(node.id),
        )
        self._record_usage(run_record, node.id, usage)

        return self._parse_verdict(str(result))

    async def _exec_tool(self, node: GraphNode, ctx: VariableContext) -> str:
        """执行 tool 节点（直接调用工具）。"""
        if self._tool_executor is None:
            raise RuntimeError("tool_executor is not set")

        # 插值参数
        params = ctx.interpolate_dict(node.params)
        self._log(f"Tool '{node.tool_name}': executing")

        result = await self._tool_executor(node.tool_name, params)
        return str(result)

    async def _exec_action(
        self,
        node: GraphNode,
        ctx: VariableContext,
        run_record: PipelineRunRecord,
    ) -> Any:
        """Invoke a plugin-contributed creative action through its typed contract."""

        action = self._actions.get(node.action_id)
        if action is None:
            raise RuntimeError(
                f"Pipeline action '{node.action_id}' is not installed"
            )
        params = ctx.interpolate_dict(node.params)
        if (
            self._unattended
            and action.side_effects == "external"
            and not bool(params.get("allow_unattended", False))
        ):
            raise RuntimeError(
                f"External action '{action.id}' requires allow_unattended=true "
                "for scheduled runs"
            )
        decision = self._node_decisions.get(node.id)
        self._used_actions.add(action.id)
        self._emit({
            "type": "action_call",
            "node_id": node.id,
            "action_id": action.id,
            "layer": action.layer,
            "side_effects": action.side_effects,
        })

        def emit(event: dict[str, Any]) -> None:
            self._emit({**event, "node_id": node.id, "action_id": action.id})

        request = PipelineActionRequest(
            action_id=action.id,
            run_id=run_record.run_id,
            node_id=node.id,
            params=params,
            trigger_input=run_record.trigger_input,
            context_outputs=run_record.context_outputs,
            attachments=tuple(
                self._resolve_node_attachments(node, ctx)
                if action.uses_model else ()
            ),
            model=decision.model if decision is not None else (
                None if is_auto(node.model) else node.model
            ),
            effort=decision.effort if decision is not None else (
                None if is_auto(node.effort) else node.effort
            ),
            unattended=self._unattended,
            emit=emit,
        )
        value = await action.invoke(request)
        if (
            isinstance(value, tuple)
            and len(value) == 2
            and isinstance(value[1], dict)
        ):
            result, usage = value
            self._record_usage(run_record, node.id, usage)
            return result
        return value

    async def _exec_skill(
        self,
        node: GraphNode,
        ctx: VariableContext,
        run_record: PipelineRunRecord,
    ) -> str:
        """执行 skill 节点。"""
        if self._skill_executor is None:
            raise RuntimeError("skill_executor is not set")

        # 插值参数
        args = ctx.interpolate_dict(node.args)
        self._log(f"Skill '{node.skill_name}': executing")

        result = await self._skill_executor(node.skill_name, args)
        if (
            isinstance(result, tuple)
            and len(result) == 2
            and isinstance(result[1], dict)
        ):
            result, usage = result
            self._record_usage(run_record, node.id, usage)
        return str(result)

    async def _exec_loop(
        self,
        node: GraphNode,
        graph: PipelineGraph,
        plan: ExecutionPlan,
        ctx: VariableContext,
        run_record: PipelineRunRecord,
    ) -> list:
        """执行 loop 节点（容器）。

        循环体节点集合来自编译期作用域（plan.loop_scopes，拓扑序），
        不再运行时朴素遍历——体外下游节点不会被误伤（循环期间不执行，
        循环结束后由外层调度沿容器边界出边推进、恰好执行一次），体内
        节点也不会被外层调度二次执行（已从外层计数/就绪中摘除）。
        """
        if node.loop_config is None:
            raise ValueError("Loop node missing loop_config")

        config = node.loop_config
        results: list[Any] = []
        body_ids = plan.loop_scopes.get(node.id, [])

        self._log(
            f"Loop: mode={config.mode.value}, "
            f"max_iterations={config.max_iterations}, body={len(body_ids)} node(s)"
        )

        for iteration in range(config.max_iterations):
            # 设置循环上下文
            ctx.set_loop_context(iteration, None)

            # 检查终止条件
            if config.mode == LoopMode.COUNT:
                if len(results) >= config.target:
                    break
            elif config.mode == LoopMode.BUDGET:
                # token 预算：优先使用 run 级预算（budget_total），否则默认 100k
                budget_cap = self._budget_total or 100_000
                used = (
                    run_record.total_input_tokens
                    + run_record.total_output_tokens
                    + run_record.total_cache_read_tokens
                    + run_record.total_cache_creation_tokens
                )
                if used > budget_cap:
                    break
            elif config.mode == LoopMode.DRY:
                if iteration >= config.dry_threshold and not results:
                    break

            # 执行循环体（嵌套容器节点由各自的 _exec_* 递归展开）
            iteration_results = []
            for body_node_id in body_ids:
                body_node = plan.node_map.get(body_node_id)
                if body_node is None:
                    continue
                try:
                    result = await self._execute_node(
                        body_node, graph, plan, ctx, run_record
                    )
                    iteration_results.append(result)
                except asyncio.CancelledError:
                    raise
                except NetworkCircuitOpenError:
                    raise  # 熔断透传：持续断网时不再继续循环
                except Exception as e:
                    err = format_error(e)
                    self._log(
                        f"Loop iteration {iteration} failed at "
                        f"'{body_node_id}': {err}"
                    )
                    self._emit({
                        "type": "warning",
                        "node_id": node.id,
                        "ref": body_node_id,
                        "message": (
                            f"循环第 {iteration + 1} 轮在节点 "
                            f"'{body_node_id}' 失败: {err}"
                        ),
                    })
                    break

            if iteration_results:
                results.extend(iteration_results)

        ctx.clear_loop_context()
        self._log(f"Loop completed: {len(results)} results")
        return results

    async def _exec_parallel(
        self,
        node: GraphNode,
        graph: PipelineGraph,
        plan: ExecutionPlan,
        ctx: VariableContext,
        run_record: PipelineRunRecord,
    ) -> list:
        """执行 parallel 节点（并发执行多个分支）。

        分支节点集合来自编译期作用域（plan.parallel_scopes）：汇合点
        及其后的节点不属于任何分支，由外层调度在全部分支完成后执行
        一次。分支失败不再被吞：整体失败并携带分支节点 id
        （ParallelBranchError）；return_exceptions 让兄弟分支先跑完
        （记录/事件完整）再抛出第一个错误。
        """
        branches = plan.parallel_scopes.get(node.id, [])
        self._log(f"Parallel: {len(branches)} branches")

        async def run_branch(branch_ids: list[str]) -> list[Any]:
            results: list[Any] = []
            for node_id in branch_ids:
                branch_node = plan.node_map.get(node_id)
                if branch_node is None:
                    continue
                try:
                    results.append(await self._execute_node(
                        branch_node, graph, plan, ctx, run_record
                    ))
                except asyncio.CancelledError:
                    raise
                except NetworkCircuitOpenError:
                    raise  # 熔断透传（不包装为分支失败），由 execute() 标记 interrupted
                except ParallelBranchError:
                    raise  # 嵌套 parallel 已带分支定位，原样上抛
                except Exception as e:
                    raise ParallelBranchError(node_id, format_error(e)) from e
            return results

        if not branches:
            return []

        all_results = await asyncio.gather(
            *(run_branch(b) for b in branches), return_exceptions=True
        )

        first_err = next(
            (r for r in all_results if isinstance(r, BaseException)), None
        )
        if first_err is not None:
            raise first_err

        # 合并结果
        results: list[Any] = []
        for r in all_results:
            results.extend(r)
        return results

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _log(self, message: str) -> None:
        """输出日志。"""
        log.info("[pipeline] %s", message)
        if self._on_log:
            self._on_log(message)
