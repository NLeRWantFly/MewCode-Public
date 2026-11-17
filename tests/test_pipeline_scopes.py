# -*- coding: utf-8 -*-
"""Pipeline 容器作用域（任务 8）与三级回退/事件流（任务 6）回归测试。

用 mock agent_factory 驱动 executor，覆盖：
- 编译期作用域：loop 体 / parallel 分支 / scope_owner
- loop：体节点每轮执行一次、体外下游循环期间不执行、结束后恰好 1 次
- parallel：分支并发、汇合点在全部分支完成后恰好执行 1 次、
  分支失败整体 failed 且错误含分支节点 id
- 三级回退：节点显式 > 图默认 > 自动挡（config 档位/内置分档表）；
  非法 effort 运行时视同 auto 回退不报错
- 辅助调用（拆解规划/格式化收尾/goal 核验/condition judgment）走轻档
- LLM 细判：置信不足区间触发且同一节点同一 run ≤ 1 次
- 事件流：所有事件带递增 seq 与 ts；node_start 带 resolved_model/
  resolved_effort/reason；store.append_run_event 落盘 events.jsonl

不依赖真实 LLM，毫秒级跑完。运行方式二选一：
    pytest tests/test_pipeline_scopes.py -q
    python tests/test_pipeline_scopes.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mewcode.config import PipelineConfig
from mewcode.pipeline.compiler import PipelineCompiler
from mewcode.pipeline.executor import ParallelBranchError, PipelineExecutor
from mewcode.pipeline.models import (
    ConditionMode,
    ExecMode,
    GraphEdge,
    GraphNode,
    LoopConfig,
    LoopMode,
    NodeType,
    PipelineGraph,
    RunStatus,
)
from mewcode.pipeline.store import PipelineStore


class RecordingFactory:
    """记录 (prompt, model, effort) 的 mock agent，可编程返回脚本。

    script 每次调用弹出一项：字符串 → 返回；异常 → 抛出；
    可调用 → fn(idx, prompt)。耗尽后重复最后一项；无脚本恒返回 "ok"。
    delay 用于并发交错验证。
    """

    def __init__(self, script=None, delay=0.0):
        self.calls: list[dict] = []
        self.script = list(script) if script else None
        self.delay = delay

    async def __call__(self, prompt, schema=None, model=None, effort=None,
                       isolation=None, on_event=None):
        idx = len(self.calls)
        self.calls.append({"prompt": prompt, "model": model, "effort": effort})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.script is None:
            return "ok", {"input_tokens": 1, "output_tokens": 1}
        item = self.script[idx] if idx < len(self.script) else self.script[-1]
        if callable(item) and not isinstance(item, BaseException):
            item = item(idx, prompt)
        if isinstance(item, BaseException):
            raise item
        return str(item), {"input_tokens": 1, "output_tokens": 1}


def run_and_collect(graph, factory, **executor_kw):
    ex = PipelineExecutor(agent_factory=factory, **executor_kw)
    events = []
    ex.on_event = lambda ev: events.append(dict(ev))
    rec = asyncio.run(ex.execute(graph, {}))
    return rec, events


def starts_of(events, node_id):
    return [
        ev for ev in events
        if ev["type"] == "node_start" and ev.get("node_id") == node_id
    ]


def idx_of(events, ev_type, node_id, which=0):
    hits = [
        i for i, ev in enumerate(events)
        if ev["type"] == ev_type and ev.get("node_id") == node_id
    ]
    return hits[which]


# ---------------------------------------------------------------------------
# 测试图
# ---------------------------------------------------------------------------


def loop_graph(target=2):
    """s→L；L→t1；t1→L（回边）；L→d（体外下游）；d→e"""
    return PipelineGraph(graph_id="g_loop_scope", name="loop-scope", nodes=[
        GraphNode(id="s", type=NodeType.START),
        GraphNode(
            id="L", type=NodeType.LOOP,
            loop_config=LoopConfig(mode=LoopMode.COUNT, target=target,
                                   max_iterations=10),
        ),
        GraphNode(id="t1", type=NodeType.TASK, goal="body task", auto_split=False),
        GraphNode(id="d", type=NodeType.TASK, goal="downstream", auto_split=False),
        GraphNode(id="e", type=NodeType.END),
    ], edges=[
        GraphEdge(id="e1", from_node="s", to_node="L"),
        GraphEdge(id="e2", from_node="L", to_node="t1"),
        GraphEdge(id="e3", from_node="t1", to_node="L"),  # 回边（闭环）
        GraphEdge(id="e4", from_node="L", to_node="d"),   # 体外出口
        GraphEdge(id="e5", from_node="d", to_node="e"),
    ])


def parallel_graph():
    """s→P；P→b1、P→b2；b1→j、b2→j（汇合点）；j→e"""
    return PipelineGraph(graph_id="g_par_scope", name="par-scope", nodes=[
        GraphNode(id="s", type=NodeType.START),
        GraphNode(id="P", type=NodeType.PARALLEL),
        GraphNode(id="b1", type=NodeType.TASK, goal="task B1", auto_split=False),
        GraphNode(id="b2", type=NodeType.TASK, goal="task B2", auto_split=False),
        GraphNode(id="j", type=NodeType.TASK, goal="join task", auto_split=False),
        GraphNode(id="e", type=NodeType.END),
    ], edges=[
        GraphEdge(id="e1", from_node="s", to_node="P"),
        GraphEdge(id="e2", from_node="P", to_node="b1"),
        GraphEdge(id="e3", from_node="P", to_node="b2"),
        GraphEdge(id="e4", from_node="b1", to_node="j"),
        GraphEdge(id="e5", from_node="b2", to_node="j"),
        GraphEdge(id="e6", from_node="j", to_node="e"),
    ])


# ---------------------------------------------------------------------------
# 编译期作用域
# ---------------------------------------------------------------------------


def test_compile_loop_scope():
    plan = PipelineCompiler.compile(loop_graph())
    assert plan.loop_scopes == {"L": ["t1"]}          # 体 = 闭环上的节点
    assert plan.scope_owner == {"t1": "L"}            # d/e 是体外下游
    assert "e3" in plan.back_edges


def test_compile_parallel_scope():
    plan = PipelineCompiler.compile(parallel_graph())
    assert plan.parallel_scopes == {"P": [["b1"], ["b2"]]}
    assert plan.scope_owner == {"b1": "P", "b2": "P"}  # j/e 是汇合点及其后


# ---------------------------------------------------------------------------
# loop：体每轮 1 次，体外下游循环后恰好 1 次
# ---------------------------------------------------------------------------


def test_loop_body_per_iteration_downstream_once():
    rec, events = run_and_collect(loop_graph(target=2), RecordingFactory())
    assert rec.status == RunStatus.COMPLETED, rec.error_message
    assert len(starts_of(events, "t1")) == 2   # 体节点执行次数 = 循环轮数
    assert len(starts_of(events, "d")) == 1    # 体外下游恰好 1 次
    assert len(starts_of(events, "e")) == 1
    assert len(starts_of(events, "L")) == 1    # 容器自身不重入
    # 下游 d 在循环全部结束（最后一次 t1 node_done）之后才开始
    last_body_done = idx_of(events, "node_done", "t1", which=-1)
    assert idx_of(events, "node_start", "d") > last_body_done


# ---------------------------------------------------------------------------
# parallel：分支并发、汇合点 1 次、分支失败携带节点 id
# ---------------------------------------------------------------------------


def test_parallel_join_after_all_branches():
    rec, events = run_and_collect(
        parallel_graph(), RecordingFactory(delay=0.05)
    )
    assert rec.status == RunStatus.COMPLETED, rec.error_message
    assert len(starts_of(events, "b1")) == 1
    assert len(starts_of(events, "b2")) == 1
    assert len(starts_of(events, "j")) == 1    # 汇合点恰好 1 次
    # 汇合点在两个分支都完成之后才开始
    j_start = idx_of(events, "node_start", "j")
    assert j_start > idx_of(events, "node_done", "b1")
    assert j_start > idx_of(events, "node_done", "b2")
    # 分支并发：b1/b2 的 node_start 都先于任一方 node_done
    max_start = max(idx_of(events, "node_start", "b1"),
                    idx_of(events, "node_start", "b2"))
    min_done = min(idx_of(events, "node_done", "b1"),
                   idx_of(events, "node_done", "b2"))
    assert max_start < min_done, "b1/b2 未并发执行"


def test_parallel_branch_failure_surfaces_node_id():
    def script(idx, prompt):
        if "task B2" in prompt:
            raise ValueError("branch exploded")
        return "ok"

    rec, events = run_and_collect(parallel_graph(), RecordingFactory([script]))
    assert rec.status == RunStatus.FAILED           # 分支失败不再被吞
    assert "b2" in (rec.error_message or "")        # 错误含分支节点 id
    assert isinstance(ParallelBranchError("x", "y"), Exception)
    # 汇合点及下游不执行
    assert len(starts_of(events, "j")) == 0


# ---------------------------------------------------------------------------
# 三级回退：节点显式 > 图默认 > 自动挡
# ---------------------------------------------------------------------------


def _task_graph(**node_kw):
    node_kw.setdefault("auto_split", False)
    node_kw.setdefault("goal", "do it")
    return PipelineGraph(graph_id="g_t", name="t", nodes=[
        GraphNode(id="s", type=NodeType.START),
        GraphNode(id="t1", type=NodeType.TASK, **node_kw),
        GraphNode(id="e", type=NodeType.END),
    ], edges=[
        GraphEdge(id="e1", from_node="s", to_node="t1"),
        GraphEdge(id="e2", from_node="t1", to_node="e"),
    ])


TIER_CFG = PipelineConfig(auto_heavy_model="heavy-x", auto_light_model="light-x")


def test_auto_plan_mode_resolves_heavy():
    factory = RecordingFactory()
    rec, events = run_and_collect(
        _task_graph(mode=ExecMode.PLAN), factory, config=TIER_CFG
    )
    assert rec.status == RunStatus.COMPLETED, rec.error_message
    ev = starts_of(events, "t1")[0]
    assert ev["resolved_model"] == "heavy-x"      # plan 必判重档
    assert ev["resolved_effort"] == "medium"      # 重档默认 medium
    assert ev["reason"]
    assert factory.calls[0]["model"] == "heavy-x"
    assert factory.calls[0]["effort"] == "medium"


def test_explicit_node_model_wins_over_auto():
    factory = RecordingFactory()
    g = _task_graph(model="my-model", effort="high", mode=ExecMode.PLAN)
    g.default_model = "graph-m"
    rec, events = run_and_collect(g, factory, config=TIER_CFG)
    ev = starts_of(events, "t1")[0]
    assert ev["resolved_model"] == "my-model"     # 显式优先级最高
    assert ev["resolved_effort"] == "high"
    assert ev["source"] == "node"
    assert factory.calls[0]["model"] == "my-model"


def test_graph_default_used_when_node_auto():
    factory = RecordingFactory()
    g = _task_graph(model="auto")
    g.default_model = "graph-m"
    rec, events = run_and_collect(g, factory, config=TIER_CFG)
    ev = starts_of(events, "t1")[0]
    assert ev["resolved_model"] == "graph-m"
    assert ev["source"] == "graph"
    assert factory.calls[0]["model"] == "graph-m"


def test_invalid_effort_falls_back_to_auto_at_runtime():
    factory = RecordingFactory()
    g = _task_graph(model="m1", effort="ultra")   # 非法 effort（保存期 warning）
    rec, events = run_and_collect(g, factory, config=TIER_CFG)
    assert rec.status == RunStatus.COMPLETED, rec.error_message  # 不报错
    ev = starts_of(events, "t1")[0]
    assert ev["resolved_model"] == "m1"           # 模型显式保留
    assert ev["resolved_effort"] == "low"         # effort 按 AUTO 回退（轻档默认）
    assert "回退" in ev["reason"]
    assert factory.calls[0]["effort"] == "low"


# ---------------------------------------------------------------------------
# 辅助调用走轻档：condition judgment / goal 核验 / 拆解规划 / 格式化收尾
# ---------------------------------------------------------------------------


def test_condition_judgment_uses_light_low():
    g = PipelineGraph(graph_id="g_c", name="c", nodes=[
        GraphNode(id="s", type=NodeType.START),
        GraphNode(id="t1", type=NodeType.TASK, goal="produce", auto_split=False),
        GraphNode(
            id="c1", type=NodeType.CONDITION,
            condition_mode=ConditionMode.JUDGMENT, ref_task="t1", criteria="ok?",
        ),
        GraphNode(id="e", type=NodeType.END),
    ], edges=[
        GraphEdge(id="e1", from_node="s", to_node="t1"),
        GraphEdge(id="e2", from_node="t1", to_node="c1"),
        GraphEdge(id="e3", from_node="c1", to_node="e", label="yes"),
        GraphEdge(id="e4", from_node="c1", to_node="e", label="no"),
    ])
    factory = RecordingFactory(["data ready", "PASS ok"])
    rec, events = run_and_collect(g, factory, config=TIER_CFG)
    assert rec.status == RunStatus.COMPLETED, rec.error_message
    ev = starts_of(events, "c1")[0]
    assert ev["resolved_model"] == "light-x"      # 判断调用走轻档
    assert ev["resolved_effort"] == "low"
    judge_call = factory.calls[1]
    assert judge_call["model"] == "light-x"
    assert judge_call["effort"] == "low"


def test_goal_check_uses_light_low():
    g = PipelineGraph(graph_id="g_g", name="g", nodes=[
        GraphNode(id="s", type=NodeType.START),
        GraphNode(id="t1", type=NodeType.TASK, goal="produce", auto_split=False,
                  model="heavy-explicit"),
        GraphNode(id="g1", type=NodeType.GOAL, criteria="done?"),
        GraphNode(id="e", type=NodeType.END),
    ], edges=[
        GraphEdge(id="e1", from_node="s", to_node="t1"),
        GraphEdge(id="e2", from_node="t1", to_node="g1"),
        GraphEdge(id="e3", from_node="g1", to_node="e"),
    ])
    factory = RecordingFactory(["made it", "PASS good"])
    rec, events = run_and_collect(g, factory, config=TIER_CFG)
    assert rec.status == RunStatus.COMPLETED, rec.error_message
    ev = starts_of(events, "g1")[0]
    assert ev["resolved_model"] == "light-x"      # goal 核验强制轻档
    assert ev["resolved_effort"] == "low"
    assert factory.calls[1]["model"] == "light-x"
    # 主任务不受影响（显式模型）
    assert factory.calls[0]["model"] == "heavy-explicit"


def test_plan_split_aux_uses_light_while_steps_use_node_decision():
    factory = RecordingFactory([
        '["step one", "step two"]',   # 拆解规划（辅助调用）
        "step one done",
        "step two done",
    ])
    g = _task_graph(model="task-model", auto_split=True)
    rec, events = run_and_collect(g, factory, config=TIER_CFG)
    assert rec.status == RunStatus.COMPLETED, rec.error_message
    assert factory.calls[0]["model"] == "light-x"       # 规划走轻档
    assert factory.calls[0]["effort"] == "low"
    assert factory.calls[1]["model"] == "task-model"    # 子步骤沿用节点决策
    assert factory.calls[2]["model"] == "task-model"
    aux = [ev for ev in events if ev["type"] == "aux_call"
           and ev["kind"] == "plan_split"]
    assert len(aux) == 1 and aux[0]["model"] == "light-x"


def test_format_finish_aux_uses_light():
    factory = RecordingFactory([
        "叙事输出，没有 JSON",
        '{"file_path": "C:/d.json"}',
    ])
    g = _task_graph(model="task-model",
                    output_schema={"file_path": "string"})
    rec, events = run_and_collect(g, factory, config=TIER_CFG)
    assert rec.status == RunStatus.COMPLETED, rec.error_message
    assert factory.calls[0]["model"] == "task-model"
    assert factory.calls[1]["model"] == "light-x"       # 收尾走轻档
    aux = [ev for ev in events if ev["type"] == "aux_call"
           and ev["kind"] == "format_finish"]
    assert len(aux) == 1


# ---------------------------------------------------------------------------
# LLM 细判：置信不足区间触发，同一 run 同一节点 ≤ 1 次
# ---------------------------------------------------------------------------


def test_llm_tier_judge_triggered_once():
    async def caps():
        return {"models": [{"id": "haiku-mini", "efforts": ["low", "high"]}],
                "tools": [], "skills": [], "modes": ["react", "plan", "go"]}

    goal = "在既有代码库中调查这个问题并给出结论。" * 10   # 100-500 字符区间
    assert 100 <= len(goal) < 500

    def script(idx, prompt):
        if "模型档位分类器" in prompt:
            return "heavy"
        return "done"

    factory = RecordingFactory([script])
    g = _task_graph(goal=goal)
    rec, events = run_and_collect(
        g, factory, capabilities_provider=caps
    )
    assert rec.status == RunStatus.COMPLETED, rec.error_message
    judge_calls = [c for c in factory.calls if "模型档位分类器" in c["prompt"]]
    assert len(judge_calls) == 1                        # 恰好一次细判
    assert judge_calls[0]["model"] == "haiku-mini"      # 细判用轻档
    assert judge_calls[0]["effort"] == "low"
    aux = [ev for ev in events if ev["type"] == "aux_call"
           and ev["kind"] == "tier_judge"]
    assert len(aux) == 1
    assert starts_of(events, "t1")[0]["source"] == "auto-llm"


# ---------------------------------------------------------------------------
# 事件流：seq/ts、resolved 字段、events.jsonl 落盘
# ---------------------------------------------------------------------------


def combined_graph():
    """菱形（x/y→d）+ loop（体 lb）+ parallel（p1/p2→j）串联"""
    return PipelineGraph(graph_id="g_all", name="all", nodes=[
        GraphNode(id="s", type=NodeType.START),
        GraphNode(id="x", type=NodeType.TASK, goal="task X", auto_split=False),
        GraphNode(id="y", type=NodeType.TASK, goal="task Y", auto_split=False),
        GraphNode(id="d", type=NodeType.TASK, goal="task D", auto_split=False),
        GraphNode(
            id="L", type=NodeType.LOOP,
            loop_config=LoopConfig(mode=LoopMode.COUNT, target=2,
                                   max_iterations=10),
        ),
        GraphNode(id="lb", type=NodeType.TASK, goal="loop body", auto_split=False),
        GraphNode(id="P", type=NodeType.PARALLEL),
        GraphNode(id="p1", type=NodeType.TASK, goal="branch 1", auto_split=False),
        GraphNode(id="p2", type=NodeType.TASK, goal="branch 2", auto_split=False),
        GraphNode(id="j", type=NodeType.TASK, goal="join", auto_split=False),
        GraphNode(id="e", type=NodeType.END),
    ], edges=[
        GraphEdge(id="e1", from_node="s", to_node="x"),
        GraphEdge(id="e2", from_node="s", to_node="y"),
        GraphEdge(id="e3", from_node="x", to_node="d"),
        GraphEdge(id="e4", from_node="y", to_node="d"),
        GraphEdge(id="e5", from_node="d", to_node="L"),
        GraphEdge(id="e6", from_node="L", to_node="lb"),
        GraphEdge(id="e7", from_node="lb", to_node="L"),   # 回边
        GraphEdge(id="e8", from_node="L", to_node="P"),
        GraphEdge(id="e9", from_node="P", to_node="p1"),
        GraphEdge(id="e10", from_node="P", to_node="p2"),
        GraphEdge(id="e11", from_node="p1", to_node="j"),
        GraphEdge(id="e12", from_node="p2", to_node="j"),
        GraphEdge(id="e13", from_node="j", to_node="e"),
    ])


def test_events_jsonl_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        store = PipelineStore(tmp)
        factory = RecordingFactory()
        ex = PipelineExecutor(agent_factory=factory, config=TIER_CFG)
        run_id_box = {}

        def on_event(ev):
            rid = run_id_box.get("run_id") or ev.get("run_id")
            if rid:
                run_id_box["run_id"] = rid
                store.append_run_event(rid, ev)

        ex.on_event = on_event
        rec = asyncio.run(ex.execute(combined_graph(), {}))
        assert rec.status == RunStatus.COMPLETED, rec.error_message

        path = Path(tmp) / ".mewcode/pipelines/runs" / f"{rec.run_id}.events.jsonl"
        assert path.exists()
        events = [json.loads(line) for line in
                  path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert events == store.load_run_events(rec.run_id)

        # 每个事件都有递增 seq 与 ts
        seqs = [ev["seq"] for ev in events]
        assert all(isinstance(s, int) for s in seqs)
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        assert all(ev.get("ts") for ev in events)

        # node_start 次数：非循环体节点恰好 1 次，循环体节点 = 轮数
        counts = {}
        for ev in events:
            if ev["type"] == "node_start":
                counts[ev["node_id"]] = counts.get(ev["node_id"], 0) + 1
        assert counts == {"s": 1, "x": 1, "y": 1, "d": 1, "L": 1,
                          "lb": 2, "P": 1, "p1": 1, "p2": 1, "j": 1, "e": 1}

        # task 节点的 node_start 携带 resolved 决策字段
        task_starts = [ev for ev in events if ev["type"] == "node_start"
                       and ev["node_id"] == "d"]
        assert task_starts[0]["resolved_model"] == "light-x"
        assert "resolved_effort" in task_starts[0]
        assert task_starts[0]["reason"]

        # edge_taken 事件含 from/to/label
        taken = [ev for ev in events if ev["type"] == "edge_taken"]
        assert taken and all(
            "from" in ev and "to" in ev and "label" in ev for ev in taken
        )
        assert any(ev["from"] == "s" and ev["to"] == "x" for ev in taken)


def test_store_append_and_load_events_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        store = PipelineStore(tmp)
        store.append_run_event("run_x", {"seq": 1, "ts": "t", "type": "run_start"})
        store.append_run_event("run_x", {"seq": 2, "ts": "t", "type": "node_start",
                                         "node_id": "a"})
        events = store.load_run_events("run_x")
        assert [e["seq"] for e in events] == [1, 2]
        assert store.load_run_events("run_missing") == []


# ---------------------------------------------------------------------------
# main：无 pytest 时直接运行
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
