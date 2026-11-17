"""Pipeline Graph 编排流的数据模型。

定义 Graph 编排流的核心数据结构：
- GraphNode: 节点（Card）
- GraphEdge: 边（连线）
- PipelineGraph: 完整图定义
- PipelineRunRecord: 执行记录
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 枚举类型
# ---------------------------------------------------------------------------


class NodeType(str, Enum):
    """节点类型。"""

    START = "start"
    SCHEDULE = "schedule"
    TASK = "task"
    GOAL = "goal"
    CONDITION = "condition"
    TOOL = "tool"
    SKILL = "skill"
    ACTION = "action"
    LOOP = "loop"
    PARALLEL = "parallel"
    END = "end"


class LoopMode(str, Enum):
    """循环模式。"""

    COUNT = "count"
    BUDGET = "budget"
    DRY = "dry"


class ExecMode(str, Enum):
    """任务执行模式（对应人类下发任务时的不同风格）。"""

    REACT = "react"
    """默认：边思考边行动的自主循环。"""
    PLAN = "plan"
    """先产出分步计划，再按计划执行。"""
    GO = "go"
    """直接快速执行，但必须在 boundary 中划定清晰边界。"""


class ConditionMode(str, Enum):
    """判断节点的核验模式。"""

    EXPRESSION = "expression"
    """指标核验：对上游结构化输出做字段比较，零 token、100% 确定性。"""
    JUDGMENT = "judgment"
    """LLM 核验：让 Agent 阅读上游输出并按标准判断 PASS/FAIL。"""


class CompareOp(str, Enum):
    """指标核验的比较运算符。"""

    EQ = "=="
    NE = "!="
    GT = ">"
    GE = ">="
    LT = "<"
    LE = "<="
    CONTAINS = "contains"


class RunStatus(str, Enum):
    """执行状态。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class NodeStatus(str, Enum):
    """节点执行状态。"""

    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# 基础模型
# ---------------------------------------------------------------------------


class Position(BaseModel):
    """节点在画布上的位置。"""

    x: float = 0
    y: float = 0


class AssetRef(BaseModel):
    """Content-addressed binary asset referenced by a pipeline graph."""

    asset: str
    mime: str
    name: str = ""



class LoopConfig(BaseModel):
    """循环配置。"""

    mode: LoopMode = LoopMode.COUNT
    target: int = 10
    """count 模式的目标数量。"""
    min_budget_per_call: int = 50_000
    """budget 模式的每轮最低 token。"""
    dry_threshold: int = 2
    """dry 模式的连续无新结果阈值。"""
    max_iterations: int = 200
    """最大轮数保护。"""


class ScheduleConfig(BaseModel):
    """定时触发配置（schedule 节点专用）。

    schedule 节点是流程中的定时关卡：左侧可接 start 或其他上游节点。
    普通流程到达时只完成布防，不会立即穿透；到点后调度器以该节点
    作为入口，沿右侧出边执行下游子图。
    """

    schedule_text: str = ""
    """用户的自然语言排期原文（如「每天早上10点」），保留供回显与重解析。"""
    resolved_cron: str = ""
    """解析后的 5 字段 cron 表达式；空 = 尚未解析，不注册触发。"""
    resolved_at: str = ""
    """一次性触发时间或间隔调度起点（UTC ISO 8601）；与 cron 二选一。"""
    resolved_interval_seconds: int = 0
    """固定间隔秒数；0 表示一次性或 cron 调度。"""
    resolved_max_runs: int = 0
    """间隔调度最多触发次数；0 表示不限制。"""
    enabled: bool = True
    """是否启用；关闭后保留配置但不触发。"""
    overlap_policy: str = "auto"
    """兼容旧图的重叠策略字段；当前运行时始终等待上一轮结束。"""
    review_enabled: bool = True
    """是否在逐轮等待真实运行结束后追加 Agent 审查。"""
    review_criteria: str = ""
    """可选的逐轮审查标准；空时根据下游任务目标自动生成。"""
    adaptive: bool = False
    """是否在每轮 run 结束后根据结果动态决定下一次触发时间。"""
    adaptive_policy: str = ""
    """自适应续约策略，例如“失败半小时后重试，成功则明天 10 点再跑”。"""


# ---------------------------------------------------------------------------
# 自动挡：语义常量与内置模型分档表
# ---------------------------------------------------------------------------


AUTO = "auto"
"""model/effort 字段的自动挡哨兵值（与 None / 空字符串等价）。"""

MODEL_TIER_HEAVY = "heavy"
"""重档：复杂任务用的高能力模型。"""
MODEL_TIER_LIGHT = "light"
"""轻档：简单任务与管线辅助调用（拆解规划/格式化/核验/条件判断）用的快速模型。"""

# 内置的「模型名子串 -> 档位」分档表：用户未在 config 的 pipeline 节配置
# auto_heavy_model / auto_light_model 时，auto_policy 用它兜底分档。
# 按子串匹配，第一个命中即生效（写法对齐 validator.MODEL_CONTEXT_WINDOWS）。
BUILTIN_MODEL_TIERS: list[tuple[str, str]] = [
    ("opus", MODEL_TIER_HEAVY),
    ("haiku", MODEL_TIER_LIGHT),
]

# 各档位的默认 reasoning effort（重档 medium / 轻档 low）。
# 最终以模型实际可用档位收敛（见 client.model_effort_levels）。
DEFAULT_TIER_EFFORT: dict[str, str] = {
    MODEL_TIER_HEAVY: "medium",
    MODEL_TIER_LIGHT: "low",
}


def is_auto(value: str | None) -> bool:
    """判断 model/effort 字段值是否为自动挡。

    None、空字符串、"auto"（不区分大小写）均视为自动挡；
    其他任意字符串视为显式覆盖。
    """
    return value is None or value.strip().lower() in ("", AUTO)


def lookup_model_tier(model: str) -> str | None:
    """按模型名子串匹配内置分档表，返回档位。

    返回 MODEL_TIER_HEAVY / MODEL_TIER_LIGHT；无法分档时返回 None
    （由调用方决定后续策略）。
    """
    m = model.lower()
    for substr, tier in BUILTIN_MODEL_TIERS:
        if substr in m:
            return tier
    return None


# ---------------------------------------------------------------------------
# GraphNode — 节点（Card）
# ---------------------------------------------------------------------------


class GraphNode(BaseModel):
    """Graph 中的一个节点（可视化 Card）。"""

    id: str = Field(default_factory=lambda: f"n_{uuid.uuid4().hex[:8]}")
    type: NodeType

    # 通用字段
    label: str = ""
    """显示标签（start/end 节点使用）。"""
    title: str = ""
    """标题（task/goal 节点使用）。"""
    position: Position = Field(default_factory=Position)

    # task 节点字段
    goal: str = ""
    """任务目标（要达成什么，必选）。"""
    prompt: str = ""
    """任务详情/补充指令（可选，空则直接用 goal）。"""
    attachments: list[AssetRef] = Field(default_factory=list)
    """Images supplied outside the text interpolation channel."""

    mode: ExecMode = ExecMode.REACT
    """执行模式：react / plan / go。"""
    boundary: str = ""
    """执行边界（go 模式必填）：允许范围、禁止操作、停止条件。"""
    tools: list[str] = Field(default_factory=list)
    """允许使用的工具列表（空 = 不限制）。"""
    skill: str | None = None
    """关联的 Skill 名称。"""
    model: str | None = None
    """模型选择。None 或 "auto"（空字符串同）= 自动挡，由三级回退解析
    （节点显式 → 图默认 → auto_policy）；其他字符串 = 显式覆盖为该模型。"""
    effort: str | None = None
    """reasoning effort 选择。None 或 "auto"（空字符串同）= 自动挡；
    其他字符串 = 显式覆盖为该档位。"""
    output_key: str = ""
    """输出变量名（用于下游引用）。"""
    auto_split: bool = True
    """内部粒子化：将大任务自动拆为子步骤逐步执行并各自重试，对外仍是一个节点。"""
    output_schema: dict[str, str] = Field(default_factory=dict)
    """可选的输出结构定义，如 {"severity": "string", "passed": "boolean"}。
    填写后 Agent 被强制输出 JSON，下游判断节点可按字段核验。"""

    # goal 节点字段
    criteria: str = ""
    """验收标准（goal 节点 / judgment 判断共用）。"""

    # condition 节点字段（双模核验）
    condition_mode: ConditionMode = ConditionMode.JUDGMENT
    """核验模式：expression(指标) / judgment(LLM)。"""
    ref_task: str = ""
    """要核验的上游任务节点 ID。"""
    # -- expression 模式专用 --
    ref_field: str = ""
    """引用的输出字段名（如 severity）。"""
    operator: CompareOp = CompareOp.EQ
    """比较运算符。"""
    compare_value: str = ""
    """期望值。"""
    # -- judgment 模式复用 criteria 字段作为核验标准 --
    expression: str = ""
    """(deprecated) 旧版条件表达式，保留兼容。"""

    # tool 节点字段
    tool_name: str = ""
    """工具名称。"""
    params: dict[str, Any] = Field(default_factory=dict)
    """工具参数。"""

    # skill 节点字段
    skill_name: str = ""
    """Skill 名称。"""
    args: dict[str, Any] = Field(default_factory=dict)
    """Skill 参数。"""

    # plugin action 节点字段
    action_id: str = ""
    """Cordis ``core.pipeline.actions`` 插槽中的稳定 Action ID。"""

    # loop 节点字段
    loop_config: LoopConfig | None = None
    """循环配置。"""

    # schedule 节点字段
    schedule: ScheduleConfig | None = None
    """定时触发配置（仅 schedule 节点使用）。"""

    # 可靠性字段
    timeout_seconds: int | None = None
    """叶子节点墙钟超时（秒）。None = 使用执行器默认值。"""

    def get_display_name(self) -> str:
        """获取显示名称。"""
        if self.title:
            return self.title
        if self.label:
            return self.label
        if self.type == NodeType.SCHEDULE and self.schedule and self.schedule.schedule_text:
            return f"⏰ {self.schedule.schedule_text}"
        if self.tool_name:
            return f"Tool: {self.tool_name}"
        if self.skill_name:
            return f"Skill: {self.skill_name}"
        if self.action_id:
            return self.title or f"Action: {self.action_id}"
        return f"{self.type.value}"


# ---------------------------------------------------------------------------
# GraphEdge — 边（连线）
# ---------------------------------------------------------------------------


class GraphEdge(BaseModel):
    """Graph 中的一条边（节点间连线）。"""

    id: str = Field(default_factory=lambda: f"e_{uuid.uuid4().hex[:8]}")
    from_node: str = Field(..., alias="from")
    """源节点 ID。"""
    to_node: str = Field(..., alias="to")
    """目标节点 ID。"""
    label: str = ""
    """边标签（condition 分支使用，口径统一为 "yes" / "no"）。"""

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# PipelineGraph — 完整图定义
# ---------------------------------------------------------------------------


class PipelineGraph(BaseModel):
    """完整的 Pipeline Graph 定义。"""

    graph_id: str = Field(default_factory=lambda: f"g_{uuid.uuid4().hex[:8]}")
    name: str
    description: str = ""
    version: int = 1

    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)

    # 图级模型默认（新字段带默认值，旧版 JSON 无需迁移即可反序列化）
    default_model: str | None = None
    """图级默认模型。None = 未设置；节点为自动挡时先回退到它，
    它也未设置再交给 auto_policy 决策。"""
    default_effort: str | None = None
    """图级默认 reasoning effort。None = 未设置，回退语义同 default_model。"""

    # 全局变量映射
    variables: dict[str, str] = Field(default_factory=dict)
    """全局变量定义，如 {"input.diff": "{{ trigger.payload }}"}。"""

    # 元信息
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def get_node(self, node_id: str) -> GraphNode | None:
        """根据 ID 获取节点。"""
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None

    def get_start_node(self) -> GraphNode | None:
        """获取起始节点。"""
        for node in self.nodes:
            if node.type == NodeType.START:
                return node
        return None

    def get_end_nodes(self) -> list[GraphNode]:
        """获取所有终止节点。"""
        return [n for n in self.nodes if n.type == NodeType.END]

    def get_outgoing_edges(self, node_id: str) -> list[GraphEdge]:
        """获取节点的所有出边。"""
        return [e for e in self.edges if e.from_node == node_id]

    def get_incoming_edges(self, node_id: str) -> list[GraphEdge]:
        """获取节点的所有入边。"""
        return [e for e in self.edges if e.to_node == node_id]

    def get_successors(self, node_id: str) -> list[str]:
        """获取节点的所有后继节点 ID。"""
        return [e.to_node for e in self.get_outgoing_edges(node_id)]

    def get_predecessors(self, node_id: str) -> list[str]:
        """获取节点的所有前驱节点 ID。"""
        return [e.from_node for e in self.get_incoming_edges(node_id)]


# ---------------------------------------------------------------------------
# NodeRunRecord — 节点执行记录
# ---------------------------------------------------------------------------


class NodeRunRecord(BaseModel):
    """单个节点的执行记录。"""

    node_id: str
    node_type: NodeType
    status: NodeStatus = NodeStatus.IDLE
    started_at: str | None = None
    completed_at: str | None = None
    result_summary: str = ""
    """结果摘要（前 500 字符）。"""
    error_message: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_usage_available: bool = False


# ---------------------------------------------------------------------------
# PipelineRunRecord — Pipeline 执行记录
# ---------------------------------------------------------------------------


class PipelineRunRecord(BaseModel):
    """Pipeline 的一次执行记录。"""

    run_id: str = Field(default_factory=lambda: f"run_{uuid.uuid4().hex[:12]}")
    graph_id: str
    graph_name: str = ""
    status: RunStatus = RunStatus.PENDING
    trigger_input: dict[str, Any] = Field(default_factory=dict)
    """触发器输入。"""
    budget_total: int | None = None
    """本次运行的 token 预算上限。"""

    node_records: dict[str, NodeRunRecord] = Field(default_factory=dict)
    """各节点的执行记录。"""

    started_at: str | None = None
    completed_at: str | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    cache_usage_available: bool = False

    context_outputs: dict[str, Any] = Field(default_factory=dict)
    """各节点的完整输出（output_key 或 node_id → 值）。
    断点续跑恢复上下文、下游取结果都依赖它。"""
    resumed_from: str | None = None
    """若本 run 是断点续跑，记录来源 run_id。"""
    entry_node_id: str | None = None
    """本次 run 的入口节点（None = 从 start 整图执行；定时触发 /
    从指定节点运行时为该节点 ID，只执行其下游可达子图）。"""

    final_output: Any = None
    """最终输出。"""
    error_message: str | None = None

    def update_node_status(
        self,
        node_id: str,
        status: NodeStatus,
        **kwargs: Any,
    ) -> None:
        """更新节点状态。"""
        if node_id not in self.node_records:
            node_type = kwargs.pop("node_type", NodeType.TASK)
            self.node_records[node_id] = NodeRunRecord(
                node_id=node_id,
                node_type=node_type,
                status=status,
                **kwargs,
            )
        else:
            record = self.node_records[node_id]
            record.status = status
            for k, v in kwargs.items():
                if hasattr(record, k):
                    setattr(record, k, v)


# ---------------------------------------------------------------------------
# API 请求/响应模型
# ---------------------------------------------------------------------------


class CreatePipelineRequest(BaseModel):
    """创建 Pipeline 请求。"""

    name: str
    description: str = ""
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    variables: dict[str, str] = Field(default_factory=dict)
    default_model: str | None = None
    """图级默认模型（语义同 PipelineGraph.default_model）。"""
    default_effort: str | None = None
    """图级默认 reasoning effort（语义同 PipelineGraph.default_effort）。"""


class UpdatePipelineRequest(BaseModel):
    """更新 Pipeline 请求。"""

    name: str | None = None
    description: str | None = None
    nodes: list[GraphNode] | None = None
    edges: list[GraphEdge] | None = None
    variables: dict[str, str] | None = None
    default_model: str | None = None
    """None = 不更新；要清除图默认请传 ""（is_auto 视其等价于未设置）。"""
    default_effort: str | None = None
    """None = 不更新；清除语义同 default_model。"""


class RunPipelineRequest(BaseModel):
    """执行 Pipeline 请求。"""

    trigger_input: dict[str, Any] = Field(default_factory=dict)
    budget_total: int | None = None
    """token 预算上限。"""
    entry_node_id: str | None = None
    """入口节点 ID：None = 从 start 整图执行；指定时从该节点向后执行子图。"""


class ValidateResponse(BaseModel):
    """校验响应。"""

    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PipelineListItem(BaseModel):
    """Pipeline 列表项。"""

    graph_id: str
    name: str
    description: str
    version: int
    node_count: int
    created_at: str
    updated_at: str
