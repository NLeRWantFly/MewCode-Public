"""Pipeline 自动挡策略模块。

节点的 model/effort 为自动挡（None/空串/"auto"，见 models.is_auto）时，
由本模块决定实际使用的模型与 reasoning effort。三级回退：

    节点显式 → 图默认（PipelineGraph.default_model/default_effort） → 自动挡

自动挡内部为双层架构（参考 harness/evolution/problem_classifier.py 的
「LLM 分析 + 本地规则降级」）：

1. 规则层（零成本秒判）：
   - condition 判断（judgment）及辅助调用（拆解规划/格式化收尾/goal 核验）
     → 轻档 + low
   - mode=plan → 重档 + medium；mode=go → 轻档 + low
   - prompt 长度 ≥ 500 字符 / 有 output_schema / 工具数 ≥ 5 → 重档 + medium
   - prompt 长度 < 100 字符 → 轻档 + low
2. LLM 细判层（规则置信不足时）：react 模式、无工具、无 output_schema、
   prompt 长度处于 100–500 字符区间 → 用轻档模型一次调用判 heavy/light。
   细判调用是可注入的 async callable（llm_judge），不可用/失败/输出无法
   解析时降级为规则默认轻档。同一 (run_id, node_id) 细判至多一次
   （结果缓存在 AutoPolicy 实例内；run_id 为 None 时不缓存，由调用方自行保证）。

档位 → 模型的映射顺序：
    config 的 pipeline.auto_heavy_model / auto_light_model
    → 内置分档表 BUILTIN_MODEL_TIERS 从可用模型列表中挑
    → 会话当前默认模型（session_model）
    → None（交由 agent_factory 的默认模型执行）

effort 最终按模型实际可用档位收敛（converge_effort，对齐
client.model_effort_levels 的口径）；档位不适用的模型（如 openai 非推理
模型）收敛为 None（不发送 effort 参数）。

阈值配置化写法参考 harness/evolution/success_detector.py：模块级默认常量
+ 构造参数覆盖。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from mewcode.pipeline.models import (
    DEFAULT_TIER_EFFORT,
    ExecMode,
    GraphNode,
    MODEL_TIER_HEAVY,
    MODEL_TIER_LIGHT,
    NodeType,
    PipelineGraph,
    is_auto,
    lookup_model_tier,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 决策来源（ModelDecision.source 的取值域）
SOURCE_NODE = "node"
"""模型由节点显式指定。"""
SOURCE_GRAPH = "graph"
"""模型来自图级默认（default_model）。"""
SOURCE_AUTO_RULE = "auto-rule"
"""自动挡规则层判定（含 LLM 细判不可用时的降级）。"""
SOURCE_AUTO_LLM = "auto-llm"
"""自动挡 LLM 细判层判定。"""

# 辅助调用类别（resolve_aux 的 kind 参数）
AUX_PLAN_SPLIT = "plan_split"
"""auto_split 内部粒子化的拆解规划调用。"""
AUX_FORMAT_FINISH = "format_finish"
"""output_schema 结构化收尾的格式化调用。"""
AUX_GOAL_CHECK = "goal_check"
"""goal 节点的验收核验调用。"""
AUX_CONDITION_JUDGMENT = "condition_judgment"
"""condition 判断（judgment）调用。注意：judgment 节点自身带 model/effort
字段（可显式覆盖），执行器应优先走 resolve(node, ...)；本类别仅作为
无节点上下文时的兜底入口。"""

_AUX_KIND_LABELS: dict[str, str] = {
    AUX_PLAN_SPLIT: "拆解规划",
    AUX_FORMAT_FINISH: "格式化收尾",
    AUX_GOAL_CHECK: "goal 核验",
    AUX_CONDITION_JUDGMENT: "条件判断",
}

# 规则层阈值默认值（可由 AutoPolicy 构造参数覆盖）
DEFAULT_HEAVY_PROMPT_CHARS = 500
"""prompt 长度达到该值 → 重档。"""
DEFAULT_LIGHT_PROMPT_CHARS = 100
"""prompt 长度低于该值 → 轻档。"""
DEFAULT_HEAVY_TOOL_COUNT = 5
"""工具数量达到该值 → 重档。"""

# effort 档位的强弱序（用于收敛时的就近匹配）
EFFORT_ORDER: tuple[str, ...] = ("off", "low", "medium", "high", "xhigh", "ultra", "max")

_TIER_LABEL: dict[str, str] = {MODEL_TIER_HEAVY: "重", MODEL_TIER_LIGHT: "轻"}

# LLM 细判调用：接收拼好的分类 prompt，返回模型原始文本输出。
# 由执行器用轻档模型构造后注入（便于测试与降级）。
LlmJudge = Callable[[str], Awaitable[str]]

TIER_JUDGE_PROMPT = (
    "你是模型档位分类器。判断下面这个流水线任务节点应使用「重档」还是「轻档」模型执行。\n"
    "重档（heavy）：需要多步推理、代码编写、复杂分析或规划的任务。\n"
    "轻档（light）：格式转换、信息提取、简短回答、机械性操作等简单任务。\n"
    "只输出一个单词：heavy 或 light，不要输出任何其他文字。\n\n"
    "任务目标：{goal}\n"
    "任务详情：{prompt}\n"
)


# ---------------------------------------------------------------------------
# 决策结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelDecision:
    """一次 model/effort 解析的决策结果。"""

    model: str | None
    """实际使用的模型。None = 无法确定，交由 agent_factory 默认模型。"""
    effort: str | None
    """实际使用的 reasoning effort（已按模型可用档位收敛）。
    None = 该模型不适用 effort 或无法确定（不发送 effort 参数）。"""
    reason: str
    """人类可读的决策理由（写进 node_start 事件与控制台 meta 行）。"""
    source: str
    """决策来源：node / graph / auto-rule / auto-llm（以模型的来源为准）。"""

    def as_event_fields(self) -> dict[str, Any]:
        """转成 node_start 事件的字段（契约：resolved_model/resolved_effort/reason）。"""
        return {
            "resolved_model": self.model,
            "resolved_effort": self.effort,
            "reason": self.reason,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# 模块级工具函数
# ---------------------------------------------------------------------------


def _pipeline_tier_models(config: Any) -> tuple[str | None, str | None]:
    """从 config 取 (auto_heavy_model, auto_light_model)。

    config 可传 AppConfig（走 .pipeline 节）、PipelineConfig 或 None。"""
    pcfg = getattr(config, "pipeline", config)
    heavy = getattr(pcfg, "auto_heavy_model", None)
    light = getattr(pcfg, "auto_light_model", None)
    return (heavy or None, light or None)


def _normalize_models(
    available_models: list[Any] | None,
) -> tuple[list[str], dict[str, list[str] | None]]:
    """把可用模型列表归一化为 (有序 id 列表, id → efforts 映射)。

    列表元素支持两种形态：
    - 纯字符串模型 id（efforts 视为未知 None）
    - capabilities 接口的 {"id": str, "efforts": [str, ...]} 字典
    """
    ids: list[str] = []
    efforts: dict[str, list[str] | None] = {}
    for item in available_models or []:
        eff: list[str] | None = None
        if isinstance(item, str):
            mid = item
        elif isinstance(item, dict):
            mid = str(item.get("id") or "")
            raw = item.get("efforts")
            if isinstance(raw, list):
                eff = [str(x) for x in raw]
        else:  # 容忍带 .id/.efforts 属性的对象
            mid = str(getattr(item, "id", "") or "")
            raw = getattr(item, "efforts", None)
            if isinstance(raw, list):
                eff = [str(x) for x in raw]
        if not mid:
            continue
        ids.append(mid)
        efforts[mid] = eff
    return ids, efforts


def _known_efforts(
    model: str | None,
    available_models: list[Any] | None = None,
    protocol: str | None = None,
) -> list[str] | None:
    """查询模型的可用 effort 档位。

    优先取 available_models 里随模型携带的 efforts；取不到且给了
    protocol 时用 client.model_effort_levels 推断（懒加载避免硬依赖）。
    返回 None = 未知（不做收敛）；[] = 该模型不适用 effort。
    """
    if not model:
        return None
    _, eff_map = _normalize_models(available_models)
    eff = eff_map.get(model)
    if eff is not None:
        return eff
    if protocol:
        try:
            from mewcode.client import model_effort_levels

            return model_effort_levels(protocol, model)
        except Exception:  # pragma: no cover - 导入/推断失败视为未知
            return None
    return None


def pick_tier_model(
    tier: str,
    config: Any,
    available_models: list[Any] | None = None,
    *,
    session_model: str | None = None,
) -> tuple[str | None, str]:
    """把档位（heavy/light）映射为具体模型。

    顺序：config 的 pipeline.auto_heavy_model/auto_light_model →
    内置分档表 BUILTIN_MODEL_TIERS 从可用模型列表中挑首个命中 →
    会话当前默认模型 → None。

    Returns:
        (模型 id 或 None, 来源说明文本)。
    """
    heavy_cfg, light_cfg = _pipeline_tier_models(config)
    label = _TIER_LABEL.get(tier, tier)
    configured = heavy_cfg if tier == MODEL_TIER_HEAVY else light_cfg
    if configured:
        return configured, f"config 配置的{label}档模型"
    ids, _ = _normalize_models(available_models)
    for mid in ids:
        if lookup_model_tier(mid) == tier:
            return mid, f"内置分档表从可用模型中选取{label}档"
    if session_model:
        return session_model, f"{label}档无匹配模型，回退会话默认模型"
    return None, f"{label}档无匹配模型且无会话默认，交由默认模型执行"


def converge_effort(
    effort: str | None,
    model: str | None,
    available_models: list[Any] | None = None,
    *,
    protocol: str | None = None,
) -> str | None:
    """把期望 effort 收敛到模型实际可用的档位（契约 8）。

    - 可用档位未知：原样返回（信任期望值）。
    - 可用档位为空列表（该模型不适用 effort）：返回 None。
    - 期望值在可用档位内：原样返回。
    - 否则按 EFFORT_ORDER 的强弱序就近匹配（距离相同时取更轻的档位）。
    """
    if effort is None:
        return None
    known = _known_efforts(model, available_models, protocol)
    if known is None:
        return effort
    if not known:
        return None
    if effort in known:
        return effort
    anchor = (
        EFFORT_ORDER.index(effort)
        if effort in EFFORT_ORDER
        else EFFORT_ORDER.index("medium")
    )
    ranked = [e for e in known if e in EFFORT_ORDER]
    if not ranked:  # 全是未知命名的自定义档位，取第一个可用值兜底
        return known[0]
    return min(ranked, key=lambda e: (abs(EFFORT_ORDER.index(e) - anchor), EFFORT_ORDER.index(e)))


# ---------------------------------------------------------------------------
# AutoPolicy
# ---------------------------------------------------------------------------


class AutoPolicy:
    """自动挡策略：规则层秒判 + LLM 细判降级，输出 ModelDecision。"""

    def __init__(
        self,
        heavy_prompt_chars: int = DEFAULT_HEAVY_PROMPT_CHARS,
        light_prompt_chars: int = DEFAULT_LIGHT_PROMPT_CHARS,
        heavy_tool_count: int = DEFAULT_HEAVY_TOOL_COUNT,
    ) -> None:
        self._heavy_chars = heavy_prompt_chars
        self._light_chars = light_prompt_chars
        self._heavy_tools = heavy_tool_count
        # LLM 细判缓存：(run_id, node_id) → (tier, reason, source)。
        # 保证同一节点同一次运行细判 ≤ 1 次（含失败降级的记录）。
        self._judge_cache: dict[tuple[str, str], tuple[str, str, str]] = {}

    # ------------------------------------------------------------------
    # 规则层
    # ------------------------------------------------------------------

    def rule_tier(
        self, node: GraphNode, prompt_text: str | None = None
    ) -> tuple[str | None, str]:
        """规则层秒判（零成本）。

        Args:
            node: 目标节点。
            prompt_text: 实际发给模型的 prompt 文本；不传则用
                node.goal + node.prompt 估算长度。

        Returns:
            (tier, reason)。tier 为 MODEL_TIER_HEAVY / MODEL_TIER_LIGHT，
            None 表示规则置信不足（进入 LLM 细判区间）。
        """
        text = prompt_text if prompt_text is not None else (node.goal + node.prompt)
        n = len(text)

        # condition 判断调用（judgment）→ 轻档
        if node.type == NodeType.CONDITION:
            return MODEL_TIER_LIGHT, "condition 判断调用"

        # 执行模式
        if node.mode == ExecMode.PLAN:
            return MODEL_TIER_HEAVY, "mode=plan 需要规划推理"
        if node.mode == ExecMode.GO:
            return MODEL_TIER_LIGHT, "mode=go 边界内快速执行"

        # 复杂度信号 → 重档
        heavy_hits: list[str] = []
        if n >= self._heavy_chars:
            heavy_hits.append(f"prompt 长度 {n} ≥ {self._heavy_chars}")
        if node.output_schema:
            heavy_hits.append("要求结构化输出 output_schema")
        if len(node.tools) >= self._heavy_tools:
            heavy_hits.append(f"工具数 {len(node.tools)} ≥ {self._heavy_tools}")
        if heavy_hits:
            return MODEL_TIER_HEAVY, "、".join(heavy_hits)

        # 短任务 → 轻档
        if n < self._light_chars:
            return MODEL_TIER_LIGHT, f"prompt 长度 {n} < {self._light_chars}"

        # 置信不足区间：react、无工具、无结构化输出、长度居中 → 交给 LLM 细判
        if node.mode == ExecMode.REACT and not node.tools and not node.output_schema:
            return None, (
                f"react 模式、无工具、无 output_schema、prompt 长度 {n} 在 "
                f"{self._light_chars}-{self._heavy_chars} 区间，规则置信不足"
            )

        return MODEL_TIER_LIGHT, "规则未命中，默认轻档"

    # ------------------------------------------------------------------
    # LLM 细判层
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_tier(text: str) -> str | None:
        """从细判输出中解析 heavy/light（都出现时取先出现者）。"""
        t = str(text).strip().lower()
        ih, il = t.find("heavy"), t.find("light")
        if ih < 0 and il < 0:
            return None
        if ih < 0:
            return MODEL_TIER_LIGHT
        if il < 0:
            return MODEL_TIER_HEAVY
        return MODEL_TIER_HEAVY if ih < il else MODEL_TIER_LIGHT

    async def _decide_tier(
        self,
        node: GraphNode,
        llm_judge: LlmJudge | None,
        run_id: str | None,
        prompt_text: str | None,
    ) -> tuple[str, str, str]:
        """决定自动挡档位。返回 (tier, reason, source)。"""
        tier, reason = self.rule_tier(node, prompt_text)
        if tier is not None:
            return tier, reason, SOURCE_AUTO_RULE

        cache_key = (run_id, node.id) if run_id else None
        if cache_key is not None and cache_key in self._judge_cache:
            c_tier, c_reason, c_source = self._judge_cache[cache_key]
            return c_tier, c_reason + "（细判结果缓存）", c_source

        if llm_judge is None:
            result = (MODEL_TIER_LIGHT, reason + "；LLM 细判不可用，降级默认轻档", SOURCE_AUTO_RULE)
        else:
            judge_prompt = TIER_JUDGE_PROMPT.format(
                goal=node.goal[:400], prompt=node.prompt[:600]
            )
            try:
                raw = await llm_judge(judge_prompt)
                parsed = self._parse_tier(raw)
                if parsed is None:
                    result = (
                        MODEL_TIER_LIGHT,
                        reason + "；LLM 细判输出无法解析，降级默认轻档",
                        SOURCE_AUTO_RULE,
                    )
                else:
                    result = (
                        parsed,
                        reason + f"；LLM 细判 → {_TIER_LABEL[parsed]}档",
                        SOURCE_AUTO_LLM,
                    )
            except Exception as e:
                log.warning("[auto_policy] LLM tier judge failed for %s: %s", node.id, e)
                result = (
                    MODEL_TIER_LIGHT,
                    reason + "；LLM 细判失败，降级默认轻档",
                    SOURCE_AUTO_RULE,
                )
        # 失败/降级也写缓存，保证同一 (run_id, node_id) 细判至多尝试一次
        if cache_key is not None:
            self._judge_cache[cache_key] = result
        return result

    def clear_judge_cache(self, run_id: str | None = None) -> None:
        """清理细判缓存。run_id=None 清空全部；否则只清该 run 的条目。

        执行器可在一次运行结束后调用，避免长驻进程缓存无界增长。"""
        if run_id is None:
            self._judge_cache.clear()
            return
        for key in [k for k in self._judge_cache if k[0] == run_id]:
            del self._judge_cache[key]

    # ------------------------------------------------------------------
    # 三级回退入口
    # ------------------------------------------------------------------

    async def resolve(
        self,
        node: GraphNode,
        graph: PipelineGraph | None,
        config: Any,
        available_models: list[Any] | None = None,
        llm_judge: LlmJudge | None = None,
        *,
        session_model: str | None = None,
        protocol: str | None = None,
        run_id: str | None = None,
        prompt_text: str | None = None,
    ) -> ModelDecision:
        """三级回退解析节点的 model/effort：节点显式 → 图默认 → 自动挡。

        非法/未知值的处理（运行时不报错，保存期由 compiler 报 warning）：
        - model/effort 为自动挡语义值（None/空串/"auto"）→ 走下一级回退；
        - 显式 effort 不在所选模型可用档位内 → 视同 auto 回退，理由记录。

        Args:
            node: 目标节点。
            graph: 所属图（取 default_model/default_effort；可为 None）。
            config: AppConfig 或 PipelineConfig（取 pipeline 档位配置；可为 None）。
            available_models: 可用模型列表（纯 id 字符串或 capabilities
                的 {"id", "efforts"} 字典，混合亦可）。
            llm_judge: LLM 细判调用（async, 分类 prompt → 原始文本）。
            session_model: 会话当前默认模型（档位无匹配时的兜底）。
            protocol: provider 协议名（anthropic/openai/openai-compat），
                用于 available_models 未携带 efforts 时推断可用档位。
            run_id: 本次运行 id（细判缓存 key；None = 不缓存）。
            prompt_text: 实际 prompt 文本（不传用 goal+prompt 估长度）。
        """
        reasons: list[str] = []
        tier: str | None = None

        # ---- 第 1 步：模型（三级回退，source 以模型来源为准）----
        model: str | None
        if not is_auto(node.model):
            model = node.model.strip()  # type: ignore[union-attr]
            source = SOURCE_NODE
            reasons.append(f"模型由节点显式指定 '{model}'")
        elif graph is not None and not is_auto(graph.default_model):
            model = graph.default_model.strip()  # type: ignore[union-attr]
            source = SOURCE_GRAPH
            reasons.append(f"节点模型为自动挡，采用图级默认 '{model}'")
        else:
            tier, tier_reason, source = await self._decide_tier(
                node, llm_judge, run_id, prompt_text
            )
            model, pick_reason = pick_tier_model(
                tier, config, available_models, session_model=session_model
            )
            label = _TIER_LABEL[tier]
            if model:
                reasons.append(
                    f"自动挡：{tier_reason} → {label}档，选用 '{model}'（{pick_reason}）"
                )
            else:
                reasons.append(f"自动挡：{tier_reason} → {label}档；{pick_reason}")

        # ---- 第 2 步：effort（同样三级回退；显式非法值视同 auto）----
        known = _known_efforts(model, available_models, protocol)
        desired: str | None = None
        if not is_auto(node.effort):
            cand = node.effort.strip().lower()  # type: ignore[union-attr]
            if known and cand not in known:
                reasons.append(
                    f"节点 effort '{cand}' 不在模型可用档位 {known} 内，视同 auto 回退"
                )
            else:
                desired = cand
                reasons.append(f"effort 由节点显式指定 '{cand}'")
        if desired is None and graph is not None and not is_auto(graph.default_effort):
            cand = graph.default_effort.strip().lower()  # type: ignore[union-attr]
            if known and cand not in known:
                reasons.append(
                    f"图默认 effort '{cand}' 不在模型可用档位 {known} 内，视同 auto 回退"
                )
            else:
                desired = cand
                reasons.append(f"effort 采用图级默认 '{cand}'")
        if desired is None:
            # effort 自动挡：优先复用模型的档位判定；模型来自节点/图时
            # 先按模型名查内置分档表，再退回规则层（不触发 LLM 细判）。
            eff_tier = tier
            if eff_tier is None and model:
                eff_tier = lookup_model_tier(model)
            if eff_tier is None:
                eff_tier = self.rule_tier(node, prompt_text)[0] or MODEL_TIER_LIGHT
            desired = DEFAULT_TIER_EFFORT[eff_tier]
            reasons.append(f"effort 自动挡：按{_TIER_LABEL[eff_tier]}档默认 '{desired}'")

        # ---- 第 3 步：按模型实际可用档位收敛（契约 8）----
        effort = converge_effort(desired, model, available_models, protocol=protocol)
        if effort != desired:
            if effort is None:
                reasons.append(f"模型 '{model}' 不适用 effort 档位，不发送该参数")
            else:
                reasons.append(f"effort '{desired}' 按模型可用档位收敛为 '{effort}'")

        return ModelDecision(
            model=model, effort=effort, reason="；".join(reasons), source=source
        )

    def resolve_aux(
        self,
        kind: str,
        config: Any,
        available_models: list[Any] | None = None,
        *,
        session_model: str | None = None,
        protocol: str | None = None,
    ) -> ModelDecision:
        """解析辅助调用的 model/effort：强制轻档 + low（零成本，无 LLM）。

        kind 取 AUX_PLAN_SPLIT / AUX_FORMAT_FINISH / AUX_GOAL_CHECK /
        AUX_CONDITION_JUDGMENT（未知值按通用辅助调用处理）。
        """
        label = _AUX_KIND_LABELS.get(kind, kind or "辅助")
        model, pick_reason = pick_tier_model(
            MODEL_TIER_LIGHT, config, available_models, session_model=session_model
        )
        desired = DEFAULT_TIER_EFFORT[MODEL_TIER_LIGHT]
        effort = converge_effort(desired, model, available_models, protocol=protocol)
        parts = [f"辅助调用（{label}）强制轻档+{desired}"]
        if model:
            parts.append(f"选用 '{model}'（{pick_reason}）")
        else:
            parts.append(pick_reason)
        if effort != desired:
            if effort is None:
                parts.append(f"模型 '{model}' 不适用 effort 档位，不发送该参数")
            else:
                parts.append(f"按模型可用档位收敛为 '{effort}'")
        return ModelDecision(
            model=model, effort=effort, reason="，".join(parts), source=SOURCE_AUTO_RULE
        )


# ---------------------------------------------------------------------------
# 模块级便捷入口（委托默认单例；细判缓存跨调用共享，key=(run_id, node_id)）
# ---------------------------------------------------------------------------

_default_policy = AutoPolicy()


async def resolve(
    node: GraphNode,
    graph: PipelineGraph | None,
    config: Any,
    available_models: list[Any] | None = None,
    llm_judge: LlmJudge | None = None,
    *,
    session_model: str | None = None,
    protocol: str | None = None,
    run_id: str | None = None,
    prompt_text: str | None = None,
) -> ModelDecision:
    """三级回退入口函数（见 AutoPolicy.resolve）。"""
    return await _default_policy.resolve(
        node,
        graph,
        config,
        available_models,
        llm_judge,
        session_model=session_model,
        protocol=protocol,
        run_id=run_id,
        prompt_text=prompt_text,
    )


def resolve_aux(
    kind: str,
    config: Any,
    available_models: list[Any] | None = None,
    *,
    session_model: str | None = None,
    protocol: str | None = None,
) -> ModelDecision:
    """辅助调用入口函数（见 AutoPolicy.resolve_aux）。"""
    return _default_policy.resolve_aux(
        kind,
        config,
        available_models,
        session_model=session_model,
        protocol=protocol,
    )
