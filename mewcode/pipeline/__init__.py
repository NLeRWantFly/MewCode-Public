"""MewCode Pipeline Graph 编排流引擎。

提供可视化 Graph 编排流功能，支持：
- Card 式节点（任务/目标/工具/条件/循环）
- JSON 图描述驱动执行
- 变量插值和条件分支
- 执行记录持久化和断点恢复
"""

from mewcode.pipeline.models import (
    CompareOp,
    ConditionMode,
    ExecMode,
    GraphEdge,
    GraphNode,
    LoopConfig,
    LoopMode,
    NodeRunRecord,
    NodeStatus,
    NodeType,
    PipelineGraph,
    PipelineListItem,
    PipelineRunRecord,
    Position,
    RunStatus,
    ScheduleConfig,
)
from mewcode.pipeline.store import PipelineStore
from mewcode.pipeline.actions import PipelineAction, PipelineActionRequest
from mewcode.pipeline.conversation import PipelineConversationAction
from mewcode.pipeline.tool_actions import (
    ALLOW_UNATTENDED_FIELD,
    PipelineToolAction,
    PipelineToolActionError,
    build_tool_actions,
    is_interactive_tool,
    merge_pipeline_actions,
    stable_tool_action_id,
    stable_tool_action_ids,
    tool_side_effects,
)
from mewcode.pipeline.schedule import (
    PipelineScheduler,
    PipelineScheduleStore,
    PipelineTrigger,
    compute_interval_fires,
    compute_next_fire,
    compute_next_fires,
)
from mewcode.pipeline.schedule_resolver import (
    ResolvedSchedule,
    SOURCE_INTERVAL,
    SOURCE_ONCE,
    describe_cron,
    resolve_schedule_text,
)
from mewcode.pipeline.next_run import (
    NextRunDecision,
    build_next_run_prompt,
    decide_by_rule as decide_next_run_by_rule,
    parse_next_run_reply,
)
from mewcode.pipeline.compiler import PipelineCompiler, ExecutionPlan, CompileError
from mewcode.pipeline.variables import VariableContext, ExpressionEvaluator
from mewcode.pipeline.executor import ParallelBranchError, PipelineExecutor
from mewcode.pipeline.auto_policy import (
    AutoPolicy,
    ModelDecision,
    resolve as resolve_model_effort,
    resolve_aux,
)

__all__ = [
    # 枚举
    "NodeType",
    "LoopMode",
    "ExecMode",
    "ConditionMode",
    "CompareOp",
    "RunStatus",
    "NodeStatus",
    # 基础模型
    "Position",
    "LoopConfig",
    "ScheduleConfig",
    # 图模型
    "GraphNode",
    "GraphEdge",
    "PipelineGraph",
    # 执行记录
    "NodeRunRecord",
    "PipelineRunRecord",
    # API 模型
    "PipelineListItem",
    "PipelineAction",
    "PipelineActionRequest",
    "PipelineConversationAction",
    "PipelineToolAction",
    "PipelineToolActionError",
    "ALLOW_UNATTENDED_FIELD",
    "build_tool_actions",
    "is_interactive_tool",
    "merge_pipeline_actions",
    "stable_tool_action_id",
    "stable_tool_action_ids",
    "tool_side_effects",
    # 存储
    "PipelineStore",
    # 定时触发
    "PipelineTrigger",
    "PipelineScheduleStore",
    "PipelineScheduler",
    "compute_interval_fires",
    "compute_next_fire",
    "compute_next_fires",
    "ResolvedSchedule",
    "SOURCE_INTERVAL",
    "SOURCE_ONCE",
    "resolve_schedule_text",
    "describe_cron",
    "NextRunDecision",
    "build_next_run_prompt",
    "decide_next_run_by_rule",
    "parse_next_run_reply",
    # 编译器
    "PipelineCompiler",
    "ExecutionPlan",
    "CompileError",
    # 变量
    "VariableContext",
    "ExpressionEvaluator",
    # 执行器
    "PipelineExecutor",
    "ParallelBranchError",
    # 自动挡策略（包级别把 auto_policy.resolve 别名为 resolve_model_effort，
    # 避免裸名 resolve 语义含糊；模块内直接 from mewcode.pipeline.auto_policy
    # import resolve 亦可）
    "AutoPolicy",
    "ModelDecision",
    "resolve_model_effort",
    "resolve_aux",
]
