# -*- coding: utf-8 -*-
"""Pipeline join 语义与就绪并发回归测试（任务 7）。

用 mock agent_factory 驱动 executor，覆盖：
- 菱形图（A→B、A→C、B→D、C→D）：汇聚点 D 恰好执行 1 次（不再重复执行）
- 就绪并发：B、C 的 node_start 都出现在任一方 node_done 之前
- 条件分支剪枝：未命中分支的下游入度被扣除，汇聚点不卡死、只执行 1 次
- 回边循环重入：第 1 轮被剪掉的分支在后续轮次命中时仍能执行（自愈）
- 编译期有效入度（ExecutionPlan.in_degree，不含回边）

不依赖真实 LLM，毫秒级跑完。运行方式二选一：
    pytest tests/test_pipeline_join.py -q
    python tests/test_pipeline_join.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mewcode.pipeline.compiler import PipelineCompiler
from mewcode.pipeline.executor import PipelineExecutor
from mewcode.pipeline.models import (
    CompareOp,
    ConditionMode,
    GraphEdge,
    GraphNode,
    NodeType,
    PipelineGraph,
    RunStatus,
)


class MockFactory:
    """带 50ms 延迟的 mock agent（延迟保证并发波内节点真正交错）。"""

    def __init__(self, script=None):
        self.calls = []
        self.script = script

    async def __call__(self, prompt, schema=None, model=None, effort=None,
                       isolation=None, on_event=None):
        idx = len(self.calls)
        self.calls.append(prompt)
        await asyncio.sleep(0.05)
        if self.script:
            item = self.script[idx] if idx < len(self.script) else self.script[-1]
            return str(item), {"input_tokens": 1, "output_tokens": 1}
        return "ok", {"input_tokens": 1, "output_tokens": 1}


def run_and_collect(graph, factory):
    """执行图并收集事件流。"""
    ex = PipelineExecutor(agent_factory=factory)
    events = []
    ex.on_event = lambda ev: events.append(dict(ev))
    rec = asyncio.run(ex.execute(graph, {}))
    return rec, events


def starts_of(events, node_id):
    return [
        ev for ev in events
        if ev["type"] == "node_start" and ev["node_id"] == node_id
    ]


def diamond_graph():
    """s→B、s→C、B→D、C→D、D→end（菱形汇聚）"""
    return PipelineGraph(graph_id="g_diamond", name="diamond", nodes=[
        GraphNode(id="s", type=NodeType.START),
        GraphNode(id="b", type=NodeType.TASK, goal="task B", auto_split=False),
        GraphNode(id="c", type=NodeType.TASK, goal="task C", auto_split=False),
        GraphNode(id="d", type=NodeType.TASK, goal="task D", auto_split=False),
        GraphNode(id="e", type=NodeType.END),
    ], edges=[
        GraphEdge(id="e1", from_node="s", to_node="b"),
        GraphEdge(id="e2", from_node="s", to_node="c"),
        GraphEdge(id="e3", from_node="b", to_node="d"),
        GraphEdge(id="e4", from_node="c", to_node="d"),
        GraphEdge(id="e5", from_node="d", to_node="e"),
    ])


# ---------------------------------------------------------------------------
# 编译期：有效入度
# ---------------------------------------------------------------------------


def test_plan_in_degree_excludes_back_edges():
    plan = PipelineCompiler.compile(diamond_graph())
    assert plan.in_degree == {"s": 0, "b": 1, "c": 1, "d": 2, "e": 1}

    # 回边不计入有效入度：t1 的回边入边被排除
    g = PipelineGraph(graph_id="g_loop", name="loop", nodes=[
        GraphNode(id="s", type=NodeType.START),
        GraphNode(id="t1", type=NodeType.TASK, goal="x", auto_split=False),
        GraphNode(
            id="c1", type=NodeType.CONDITION,
            condition_mode=ConditionMode.JUDGMENT, ref_task="t1", criteria="ok?",
        ),
        GraphNode(id="e", type=NodeType.END),
    ], edges=[
        GraphEdge(id="e1", from_node="s", to_node="t1"),
        GraphEdge(id="e2", from_node="t1", to_node="c1"),
        GraphEdge(id="e3", from_node="c1", to_node="e", label="yes"),
        GraphEdge(id="e4", from_node="c1", to_node="t1", label="no"),  # 回边
    ])
    plan = PipelineCompiler.compile(g)
    assert plan.in_degree == {"s": 0, "t1": 1, "c1": 1, "e": 1}
    assert "e4" in plan.back_edges


# ---------------------------------------------------------------------------
# 菱形 join + 就绪并发
# ---------------------------------------------------------------------------


def test_diamond_join_executes_once():
    rec, events = run_and_collect(diamond_graph(), MockFactory())
    assert rec.status == RunStatus.COMPLETED, rec.error_message
    # 验收：D 的 node_start 恰好 1 次（旧实现会执行 2 次）
    assert len(starts_of(events, "d")) == 1
    assert len(starts_of(events, "b")) == 1
    assert len(starts_of(events, "c")) == 1


def test_diamond_branches_run_concurrently():
    rec, events = run_and_collect(diamond_graph(), MockFactory())
    assert rec.status == RunStatus.COMPLETED, rec.error_message
    # 验收：B、C 的 node_start 都出现在任一方 node_done 之前
    seq = [
        (ev["type"], ev["node_id"]) for ev in events
        if ev["type"] in ("node_start", "node_done")
        and ev.get("node_id") in ("b", "c")
    ]
    start_idx = [i for i, (t, _) in enumerate(seq) if t == "node_start"]
    done_idx = [i for i, (t, _) in enumerate(seq) if t == "node_done"]
    assert max(start_idx) < min(done_idx), f"B/C 未并发执行: {seq}"


# ---------------------------------------------------------------------------
# 条件分支剪枝：被剪掉的前驱从汇聚点计数中扣除
# ---------------------------------------------------------------------------


def test_condition_prune_unblocks_join():
    """s→cond, cond-yes→B, cond-no→C, B→D, C→D, D→end。

    yes 命中：C 被剪掉，其对 D 的入度贡献被扣除，
    D 仍恰好执行 1 次（而不是永远等待 C）。
    """
    g = PipelineGraph(graph_id="g_cd", name="cond-diamond", nodes=[
        GraphNode(id="s", type=NodeType.START),
        GraphNode(
            id="cond", type=NodeType.CONDITION,
            condition_mode=ConditionMode.EXPRESSION,
            ref_task="s", ref_field="status",
            operator=CompareOp.EQ, compare_value="started",
        ),
        GraphNode(id="b", type=NodeType.TASK, goal="task B", auto_split=False),
        GraphNode(id="c", type=NodeType.TASK, goal="task C", auto_split=False),
        GraphNode(id="d", type=NodeType.TASK, goal="task D", auto_split=False),
        GraphNode(id="e", type=NodeType.END),
    ], edges=[
        GraphEdge(id="e1", from_node="s", to_node="cond"),
        GraphEdge(id="e2", from_node="cond", to_node="b", label="yes"),
        GraphEdge(id="e3", from_node="cond", to_node="c", label="no"),
        GraphEdge(id="e4", from_node="b", to_node="d"),
        GraphEdge(id="e5", from_node="c", to_node="d"),
        GraphEdge(id="e6", from_node="d", to_node="e"),
    ])
    rec, events = run_and_collect(g, MockFactory())
    assert rec.status == RunStatus.COMPLETED, rec.error_message
    assert len(starts_of(events, "b")) == 1   # 命中分支执行
    assert len(starts_of(events, "c")) == 0   # 未命中分支被剪掉
    assert len(starts_of(events, "d")) == 1   # 汇聚点就绪且只执行 1 次
    assert len(starts_of(events, "e")) == 1


# ---------------------------------------------------------------------------
# 回边循环重入 + 剪枝自愈
# ---------------------------------------------------------------------------


def test_loop_reentry_heals_pruned_branch():
    """s→t1→cond, cond-no→t1(回边), cond-yes→end。

    第 1 轮 no：yes 分支的 end 被剪；第 2 轮 yes：end 仍能被唤醒执行。
    """
    g = PipelineGraph(graph_id="g_lp", name="loop", nodes=[
        GraphNode(
            id="s", type=NodeType.START),
        GraphNode(
            id="t1", type=NodeType.TASK, goal="produce n", auto_split=False,
            output_schema={"n": "int"},
        ),
        GraphNode(
            id="cond", type=NodeType.CONDITION,
            condition_mode=ConditionMode.EXPRESSION,
            ref_task="t1", ref_field="n",
            operator=CompareOp.GE, compare_value="2",
        ),
        GraphNode(id="e", type=NodeType.END),
    ], edges=[
        GraphEdge(id="e1", from_node="s", to_node="t1"),
        GraphEdge(id="e2", from_node="t1", to_node="cond"),
        GraphEdge(id="e3", from_node="cond", to_node="e", label="yes"),
        GraphEdge(id="e4", from_node="cond", to_node="t1", label="no"),
    ])
    factory = MockFactory(script=['{"n": 1}', '{"n": 2}'])
    rec, events = run_and_collect(g, factory)
    assert rec.status == RunStatus.COMPLETED, rec.error_message
    assert len(starts_of(events, "t1")) == 2    # 回边重入执行 2 次
    assert len(starts_of(events, "cond")) == 2
    assert len(starts_of(events, "e")) == 1     # 被剪过的 end 自愈执行
    assert len(factory.calls) == 2


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
