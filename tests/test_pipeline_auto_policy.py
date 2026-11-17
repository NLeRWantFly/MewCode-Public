"""Pipeline 自动挡策略（auto_policy）测试——规则层秒判、三级回退、LLM 细判降级与缓存。"""
from __future__ import annotations

import asyncio

from mewcode.pipeline.auto_policy import (
    AutoPolicy,
    SOURCE_AUTO_LLM,
    SOURCE_AUTO_RULE,
    SOURCE_GRAPH,
    SOURCE_NODE,
    AUX_PLAN_SPLIT,
    converge_effort,
    pick_tier_model,
    resolve_aux,
)
from mewcode.pipeline.models import (
    MODEL_TIER_HEAVY,
    MODEL_TIER_LIGHT,
    ExecMode,
    GraphNode,
    NodeType,
    PipelineGraph,
)


def _task(**kw) -> GraphNode:
    base = dict(type=NodeType.TASK, title="t", goal="", prompt="", mode=ExecMode.REACT)
    base.update(kw)
    return GraphNode(**base)


def _graph(**kw) -> PipelineGraph:
    return PipelineGraph(name="g", **kw)


def _resolve(policy: AutoPolicy, node, graph=None, **kw):
    return asyncio.run(policy.resolve(node, graph, None, **kw))


# ---------------------------------------------------------------------------
# 规则层秒判
# ---------------------------------------------------------------------------

class TestRuleTier:
    def setup_method(self) -> None:
        self.p = AutoPolicy()

    def test_condition_judgment_is_light(self) -> None:
        node = GraphNode(type=NodeType.CONDITION, criteria="上游输出包含 done")
        tier, reason = self.p.rule_tier(node)
        assert tier == MODEL_TIER_LIGHT
        assert "condition" in reason

    def test_mode_plan_is_heavy(self) -> None:
        tier, _ = self.p.rule_tier(_task(mode=ExecMode.PLAN, goal="x" * 200))
        assert tier == MODEL_TIER_HEAVY

    def test_mode_go_is_light(self) -> None:
        # go 优先于长 prompt 的重档规则
        tier, _ = self.p.rule_tier(_task(mode=ExecMode.GO, goal="x" * 800))
        assert tier == MODEL_TIER_LIGHT

    def test_long_prompt_is_heavy(self) -> None:
        tier, reason = self.p.rule_tier(_task(goal="x" * 500))
        assert tier == MODEL_TIER_HEAVY
        assert "500" in reason

    def test_output_schema_is_heavy(self) -> None:
        node = _task(goal="x" * 150, output_schema={"passed": "boolean"})
        tier, _ = self.p.rule_tier(node)
        assert tier == MODEL_TIER_HEAVY

    def test_many_tools_is_heavy(self) -> None:
        node = _task(goal="x" * 150, tools=["a", "b", "c", "d", "e"])
        tier, _ = self.p.rule_tier(node)
        assert tier == MODEL_TIER_HEAVY

    def test_short_prompt_is_light(self) -> None:
        tier, _ = self.p.rule_tier(_task(goal="整理输出"))
        assert tier == MODEL_TIER_LIGHT

    def test_uncertain_zone_returns_none(self) -> None:
        # react、无工具、无 schema、100-500 字符 → 置信不足
        tier, reason = self.p.rule_tier(_task(goal="x" * 200))
        assert tier is None
        assert "置信不足" in reason

    def test_midrange_with_few_tools_defaults_light(self) -> None:
        # 不满足置信不足区间（有工具）也不满足任何重档规则 → 默认轻档
        tier, _ = self.p.rule_tier(_task(goal="x" * 200, tools=["bash"]))
        assert tier == MODEL_TIER_LIGHT

    def test_threshold_override(self) -> None:
        p = AutoPolicy(heavy_prompt_chars=50)
        tier, _ = p.rule_tier(_task(goal="x" * 60))
        assert tier == MODEL_TIER_HEAVY

    def test_prompt_text_overrides_node_fields(self) -> None:
        tier, _ = self.p.rule_tier(_task(goal="短"), prompt_text="y" * 600)
        assert tier == MODEL_TIER_HEAVY


# ---------------------------------------------------------------------------
# 档位 → 模型映射 与 effort 收敛
# ---------------------------------------------------------------------------

class _Cfg:
    def __init__(self, heavy=None, light=None):
        self.auto_heavy_model = heavy
        self.auto_light_model = light


class TestTierModelAndEffort:
    def test_config_wins(self) -> None:
        model, _ = pick_tier_model(MODEL_TIER_HEAVY, _Cfg(heavy="my-big"), ["claude-opus-4"])
        assert model == "my-big"

    def test_builtin_tiers_from_available(self) -> None:
        models = ["gpt-4o", "claude-opus-4", "claude-haiku-3"]
        heavy, _ = pick_tier_model(MODEL_TIER_HEAVY, None, models)
        light, _ = pick_tier_model(MODEL_TIER_LIGHT, None, models)
        assert heavy == "claude-opus-4"
        assert light == "claude-haiku-3"

    def test_session_model_fallback_then_none(self) -> None:
        model, _ = pick_tier_model(MODEL_TIER_LIGHT, None, ["gpt-4o"], session_model="sess-m")
        assert model == "sess-m"
        model2, _ = pick_tier_model(MODEL_TIER_LIGHT, None, ["gpt-4o"])
        assert model2 is None

    def test_converge_effort(self) -> None:
        caps = [{"id": "m1", "efforts": ["off", "low", "medium", "high"]},
                {"id": "m2", "efforts": []}]
        assert converge_effort("medium", "m1", caps) == "medium"
        assert converge_effort("ultra", "m1", caps) == "high"  # 就近收敛
        assert converge_effort("low", "m2", caps) is None      # 档位不适用
        assert converge_effort("low", "unknown", caps) == "low"  # 未知模型信任原值


# ---------------------------------------------------------------------------
# 三级回退 resolve
# ---------------------------------------------------------------------------

class TestResolve:
    def setup_method(self) -> None:
        self.p = AutoPolicy()

    def test_node_explicit_wins(self) -> None:
        node = _task(model="claude-opus-4", effort="high", mode=ExecMode.GO)
        d = _resolve(self.p, node, _graph(default_model="g-model"))
        assert d.model == "claude-opus-4"
        assert d.effort == "high"
        assert d.source == SOURCE_NODE

    def test_graph_default_second(self) -> None:
        node = _task(model="auto", goal="短目标")
        d = _resolve(self.p, node, _graph(default_model="g-model", default_effort="high"))
        assert d.model == "g-model"
        assert d.effort == "high"
        assert d.source == SOURCE_GRAPH

    def test_auto_rule_third(self) -> None:
        node = _task(mode=ExecMode.PLAN, goal="规划一个方案")
        d = _resolve(self.p, node, None, available_models=["claude-opus-4", "claude-haiku-3"])
        assert d.model == "claude-opus-4"
        assert d.effort == "medium"  # 重档默认 medium
        assert d.source == SOURCE_AUTO_RULE
        assert d.reason

    def test_invalid_explicit_effort_falls_back_to_auto(self) -> None:
        caps = [{"id": "claude-haiku-3", "efforts": ["off", "low", "medium", "high"]}]
        node = _task(model="claude-haiku-3", effort="ultra", goal="整理")
        d = _resolve(self.p, node, None, available_models=caps)
        assert d.model == "claude-haiku-3"
        assert d.effort == "low"  # 视同 auto → haiku 轻档默认 low
        assert "ultra" in d.reason

    def test_effort_converges_to_empty_levels(self) -> None:
        caps = [{"id": "gpt-4o", "efforts": []}]
        node = _task(model="gpt-4o", effort="medium")
        d = _resolve(self.p, node, None, available_models=caps)
        assert d.effort is None

    def test_event_fields(self) -> None:
        node = _task(model="m", effort="low")
        d = _resolve(self.p, node, None)
        fields = d.as_event_fields()
        assert fields["resolved_model"] == "m"
        assert fields["resolved_effort"] == "low"
        assert fields["reason"]


# ---------------------------------------------------------------------------
# LLM 细判：触发、降级、缓存 ≤1 次
# ---------------------------------------------------------------------------

class TestLlmJudge:
    def _uncertain_node(self, **kw) -> GraphNode:
        return _task(goal="x" * 200, **kw)

    def test_judge_heavy(self) -> None:
        p = AutoPolicy()
        calls: list[str] = []

        async def judge(prompt: str) -> str:
            calls.append(prompt)
            return "heavy"

        d = _resolve(p, self._uncertain_node(), None,
                     available_models=["claude-opus-4", "claude-haiku-3"], llm_judge=judge)
        assert d.model == "claude-opus-4"
        assert d.source == SOURCE_AUTO_LLM
        assert len(calls) == 1

    def test_judge_unavailable_degrades_light(self) -> None:
        d = _resolve(AutoPolicy(), self._uncertain_node(), None,
                     available_models=["claude-haiku-3"])
        assert d.model == "claude-haiku-3"
        assert d.source == SOURCE_AUTO_RULE
        assert "降级" in d.reason

    def test_judge_error_degrades_light(self) -> None:
        async def judge(prompt: str) -> str:
            raise RuntimeError("boom")

        d = _resolve(AutoPolicy(), self._uncertain_node(), None,
                     available_models=["claude-haiku-3"], llm_judge=judge)
        assert d.model == "claude-haiku-3"
        assert d.source == SOURCE_AUTO_RULE

    def test_judge_cached_per_run_and_node(self) -> None:
        p = AutoPolicy()
        count = 0

        async def judge(prompt: str) -> str:
            nonlocal count
            count += 1
            return "light"

        node = self._uncertain_node()
        for _ in range(3):
            _resolve(p, node, None, llm_judge=judge, run_id="run_1")
        assert count == 1  # 同一 (run_id, node_id) 细判至多一次
        _resolve(p, node, None, llm_judge=judge, run_id="run_2")
        assert count == 2  # 新 run 重新细判
        p.clear_judge_cache("run_1")
        _resolve(p, node, None, llm_judge=judge, run_id="run_1")
        assert count == 3


# ---------------------------------------------------------------------------
# 辅助调用强制轻档
# ---------------------------------------------------------------------------

class TestResolveAux:
    def test_aux_is_light_low(self) -> None:
        d = resolve_aux(AUX_PLAN_SPLIT, _Cfg(light="my-fast"), ["claude-opus-4"])
        assert d.model == "my-fast"
        assert d.effort == "low"
        assert d.source == SOURCE_AUTO_RULE
        assert "拆解规划" in d.reason
