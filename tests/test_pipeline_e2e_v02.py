# -*- coding: utf-8 -*-
"""Pipeline v0.2 任务 15 端到端验证（回归资产）。

覆盖 checklist「端到端」两条验收：

1. 综合验收（后端可机械验证部分）：
   条件分支 + 循环（condition no 回边重试）+ 菱形汇聚 的管线，
   三个 task 节点分别配 显式模型 / 图默认 / AUTO，执行后断言：
   - events.jsonl 存在；所有事件带递增 seq 与 ts
   - 非循环体节点 node_start 恰好 1 次；循环体节点次数 = 循环轮数
   - node_start.resolved_model 分别为 显式值 / 图默认值 / 自动挡解析值，
     source 分别是 node / graph / auto-*
     （注：三级回退设计使 graph 与 auto-* 在同一次 run 内互斥——
     default_model 一旦设置，所有 AUTO 节点都命中图默认。因此用
     同一张图两跑覆盖：run A 图默认为空 → auto-rule/auto-llm；
     run B 图默认非空 → graph。）
   - 菱形 B/C 并发（双方 node_start 均在任一方 node_done 之前）
   - edge_taken 事件存在且分支标签口径为 yes/no

2. 兼容验收：v0.1 形态 JSON（无 model/effort/default_* 字段、边标签
   yes/no）直接落盘 → store 加载 → validate → compile → API 直接编辑
   并运行成功，空值按 AUTO 解析。

另附「模型与自动挡」「执行正确性」「易用性」中机械可验项的 API 冒烟：
capabilities / validate / 内联试跑 / import / export / duplicate /
PUT 乐观锁 409 / 非法 effort 保存 warning / 变量引用 warning。

不依赖真实 LLM。运行方式二选一：
    pytest tests/test_pipeline_e2e_v02.py -q
    python tests/test_pipeline_e2e_v02.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mewcode.client import model_effort_levels
from mewcode.config import PipelineConfig, load_config
from mewcode.pipeline.compiler import PipelineCompiler
from mewcode.pipeline.executor import PipelineExecutor
from mewcode.pipeline.models import (
    ConditionMode,
    GraphEdge,
    GraphNode,
    NodeType,
    PipelineGraph,
    RunStatus,
    lookup_model_tier,
)
from mewcode.pipeline.store import PipelineStore

TIER_CFG = PipelineConfig(auto_heavy_model="heavy-x", auto_light_model="light-x")

# 100–500 字符区间的目标文本（react、无 tools、无 schema → LLM 细判区）
MID_GOAL = "分析上游产物的结构并给出关键结论，说明依据与局限。" * 6
assert 100 <= len(MID_GOAL) < 500

# 循环轮数：judgment 第 1 次 FAIL（走 no 回边重试）、第 2 次 PASS → 共 2 轮
LOOP_ROUNDS = 2


class E2EFactory:
    """按 prompt 内容路由的 mock agent（并发窗口用 delay 拉开）。

    - 自动挡 LLM 细判（含「模型档位分类器」）→ 立即返回 judge_tier
    - condition judgment（含 criteria 'artifact ok?'）→ 前 fail_times 次
      FAIL，之后 PASS（驱动 no 回边重试循环）
    - 其余 task 调用 → sleep(delay) 后返回 "ok"
    """

    def __init__(self, judge_tier: str = "heavy", fail_times: int = 1,
                 delay: float = 0.05):
        self.calls: list[dict] = []
        self.judge_tier = judge_tier
        self.fail_times = fail_times
        self.delay = delay
        self._cond_seen = 0

    async def __call__(self, prompt, schema=None, model=None, effort=None,
                       isolation=None, on_event=None):
        self.calls.append({"prompt": prompt, "model": model, "effort": effort})
        usage = {"input_tokens": 1, "output_tokens": 1}
        if "模型档位分类器" in prompt:
            return self.judge_tier, usage
        if "artifact ok?" in prompt:
            self._cond_seen += 1
            if self._cond_seen <= self.fail_times:
                return "FAIL 还没有产物", usage
            return "PASS 产物齐了", usage
        if self.delay:
            await asyncio.sleep(self.delay)
        return "ok", usage


def e2e_graph(default_model: str | None = None) -> PipelineGraph:
    """条件分支 + 循环（no 回边）+ 菱形汇聚。

    s → tA →(每轮) c1；c1 --no--> tA（回边循环）；
    c1 --yes--> tB、tC（菱形分叉）；tB/tC → tD（汇聚）→ e。
    三个 task 节点：tA 显式模型；tB/tC AUTO（图默认设置时命中图默认）。
    """
    return PipelineGraph(
        graph_id="g_e2e_v02", name="e2e-v02", default_model=default_model,
        nodes=[
            GraphNode(id="s", type=NodeType.START),
            GraphNode(id="tA", type=NodeType.TASK, title="A-explicit",
                      goal="produce artifact", model="explicit-m",
                      effort="high", auto_split=False),
            GraphNode(id="c1", type=NodeType.CONDITION,
                      condition_mode=ConditionMode.JUDGMENT,
                      ref_task="tA", criteria="artifact ok?"),
            GraphNode(id="tB", type=NodeType.TASK, title="B-auto-short",
                      goal="collect left", auto_split=False),
            GraphNode(id="tC", type=NodeType.TASK, title="C-auto-mid",
                      goal=MID_GOAL, auto_split=False),
            GraphNode(id="tD", type=NodeType.TASK, title="D-join",
                      goal="merge results", auto_split=False),
            GraphNode(id="e", type=NodeType.END),
        ],
        edges=[
            GraphEdge(id="e_sa", from_node="s", to_node="tA"),
            GraphEdge(id="e_ac", from_node="tA", to_node="c1"),
            GraphEdge(id="e_no", from_node="c1", to_node="tA", label="no"),
            GraphEdge(id="e_yb", from_node="c1", to_node="tB", label="yes"),
            GraphEdge(id="e_yc", from_node="c1", to_node="tC", label="yes"),
            GraphEdge(id="e_bd", from_node="tB", to_node="tD"),
            GraphEdge(id="e_cd", from_node="tC", to_node="tD"),
            GraphEdge(id="e_de", from_node="tD", to_node="e"),
        ],
    )


def run_with_store(graph, factory, tmp, run_id, config=TIER_CFG, **executor_kw):
    """执行一次并把事件流落盘到 tmp 下的 events.jsonl。"""
    store = PipelineStore(tmp)
    ex = PipelineExecutor(agent_factory=factory, config=config, **executor_kw)
    ex.on_event = lambda ev: store.append_run_event(run_id, ev)
    rec = asyncio.run(ex.execute(graph, {"topic": "e2e"}, run_id=run_id))
    path = Path(tmp) / ".mewcode/pipelines/runs" / f"{run_id}.events.jsonl"
    return rec, store.load_run_events(run_id), path


def starts_of(events, node_id):
    return [ev for ev in events
            if ev["type"] == "node_start" and ev.get("node_id") == node_id]


def idx_of(events, ev_type, node_id, which=0):
    hits = [i for i, ev in enumerate(events)
            if ev["type"] == ev_type and ev.get("node_id") == node_id]
    return hits[which]


def node_start_counts(events):
    counts: dict[str, int] = {}
    for ev in events:
        if ev["type"] == "node_start":
            counts[ev["node_id"]] = counts.get(ev["node_id"], 0) + 1
    return counts


def assert_common_e2e_shape(events, path):
    """两次 run 共用的结构断言：文件、seq/ts、次数、并发、edge_taken。"""
    # events.jsonl 存在且逐行可解析
    assert path.exists(), f"events.jsonl 不存在: {path}"
    lines = [json.loads(x) for x in
             path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert lines == events

    # 所有事件带递增 seq 与 ts
    seqs = [ev["seq"] for ev in events]
    assert all(isinstance(s, int) for s in seqs)
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert all(ev.get("ts") for ev in events)

    # 非循环体节点恰好 1 次；循环体节点（tA/c1）= 循环轮数
    assert node_start_counts(events) == {
        "s": 1, "tA": LOOP_ROUNDS, "c1": LOOP_ROUNDS,
        "tB": 1, "tC": 1, "tD": 1, "e": 1,
    }

    # 菱形 B/C 并发：双方 node_start 均在任一方 node_done 之前
    max_start = max(idx_of(events, "node_start", "tB"),
                    idx_of(events, "node_start", "tC"))
    min_done = min(idx_of(events, "node_done", "tB"),
                   idx_of(events, "node_done", "tC"))
    assert max_start < min_done, "tB/tC 未并发执行"

    # 汇聚点 tD 在两个分支都完成后才开始
    d_start = idx_of(events, "node_start", "tD")
    assert d_start > idx_of(events, "node_done", "tB")
    assert d_start > idx_of(events, "node_done", "tC")

    # edge_taken：契约字段齐全，分支标签口径 yes/no
    taken = [ev for ev in events if ev["type"] == "edge_taken"]
    assert taken and all(
        "edge_id" in ev and "from" in ev and "to" in ev and "label" in ev
        for ev in taken
    )
    by_edge: dict[str, list] = {}
    for ev in taken:
        by_edge.setdefault(ev["edge_id"], []).append(ev)
    assert len(by_edge["e_no"]) == 1 and by_edge["e_no"][0]["label"] == "no"
    assert len(by_edge["e_yb"]) == 1 and by_edge["e_yb"][0]["label"] == "yes"
    assert len(by_edge["e_yc"]) == 1 and by_edge["e_yc"][0]["label"] == "yes"
    labels = {ev["label"] for ev in taken if ev["label"]}
    assert labels == {"yes", "no"}


# ---------------------------------------------------------------------------
# 综合验收 run A：图默认为空 → 显式 / auto-rule / auto-llm
# ---------------------------------------------------------------------------


def test_e2e_run_a_explicit_and_auto_sources():
    factory = E2EFactory(judge_tier="heavy")
    with tempfile.TemporaryDirectory() as tmp:
        rec, events, path = run_with_store(
            e2e_graph(default_model=None), factory, tmp, "run_e2e_a"
        )
        assert rec.status == RunStatus.COMPLETED, rec.error_message
        assert_common_e2e_shape(events, path)

        # tA：节点显式（两轮都一样）
        for ev in starts_of(events, "tA"):
            assert ev["resolved_model"] == "explicit-m"
            assert ev["resolved_effort"] == "high"
            assert ev["source"] == "node"
            assert ev["reason"]

        # tB：短 goal → 自动挡规则层轻档
        b = starts_of(events, "tB")[0]
        assert b["resolved_model"] == "light-x"
        assert b["resolved_effort"] == "low"
        assert b["source"] == "auto-rule"
        assert b["reason"]

        # tC：置信不足区间 → LLM 细判 heavy → 重档 medium
        c = starts_of(events, "tC")[0]
        assert c["resolved_model"] == "heavy-x"
        assert c["resolved_effort"] == "medium"
        assert c["source"] == "auto-llm"

        # 细判是轻档辅助调用：恰好一次 aux_call(kind=tier_judge)
        judges = [ev for ev in events if ev["type"] == "aux_call"
                  and ev["kind"] == "tier_judge"]
        assert len(judges) == 1
        assert judges[0]["node_id"] == "tC"
        assert judges[0]["model"] == "light-x"
        assert judges[0]["effort"] == "low"

        # condition judgment 走轻档 + low
        for ev in starts_of(events, "c1"):
            assert ev["resolved_model"] == "light-x"
            assert ev["resolved_effort"] == "low"

        # 决策贯穿到实际 agent 调用参数
        models_used = {}
        for call in factory.calls:
            if "produce artifact" in call["prompt"] and "核验" not in call["prompt"]:
                models_used.setdefault("tA", call)
            elif "collect left" in call["prompt"]:
                models_used.setdefault("tB", call)
            elif MID_GOAL[:20] in call["prompt"] and "模型档位分类器" not in call["prompt"]:
                models_used.setdefault("tC", call)
        assert models_used["tA"]["model"] == "explicit-m"
        assert models_used["tA"]["effort"] == "high"
        assert models_used["tB"]["model"] == "light-x"
        assert models_used["tC"]["model"] == "heavy-x"


# ---------------------------------------------------------------------------
# 综合验收 run B：图默认非空 → AUTO 节点实际用图默认（source=graph）
# ---------------------------------------------------------------------------


def test_e2e_run_b_graph_default_applies():
    factory = E2EFactory()
    with tempfile.TemporaryDirectory() as tmp:
        rec, events, path = run_with_store(
            e2e_graph(default_model="graph-m"), factory, tmp, "run_e2e_b"
        )
        assert rec.status == RunStatus.COMPLETED, rec.error_message
        assert_common_e2e_shape(events, path)

        # 显式节点不被改写（优先级最高）
        assert starts_of(events, "tA")[0]["resolved_model"] == "explicit-m"
        assert starts_of(events, "tA")[0]["source"] == "node"

        # AUTO 节点全部命中图默认（含原本会走 auto-llm 的 tC——
        # 三级回退：图默认优先于自动挡，二者同 run 互斥）
        for nid in ("tB", "tC", "tD"):
            ev = starts_of(events, nid)[0]
            assert ev["resolved_model"] == "graph-m", (nid, ev)
            assert ev["source"] == "graph", (nid, ev)

        # 图默认命中时不触发 LLM 细判
        assert not [ev for ev in events if ev["type"] == "aux_call"
                    and ev["kind"] == "tier_judge"]


# ---------------------------------------------------------------------------
# 非法 effort 的图直接运行：不报错，events.jsonl 中按 AUTO 回退并附理由
# ---------------------------------------------------------------------------


def test_invalid_effort_runs_with_fallback_in_events():
    g = PipelineGraph(graph_id="g_bad_eff", name="bad-eff", nodes=[
        GraphNode(id="s", type=NodeType.START),
        GraphNode(id="t1", type=NodeType.TASK, goal="do it",
                  model="m1", effort="ultra", auto_split=False),
        GraphNode(id="e", type=NodeType.END),
    ], edges=[
        GraphEdge(id="e1", from_node="s", to_node="t1"),
        GraphEdge(id="e2", from_node="t1", to_node="e"),
    ])
    factory = E2EFactory(delay=0)
    with tempfile.TemporaryDirectory() as tmp:
        rec, events, path = run_with_store(g, factory, tmp, "run_bad_eff")
        assert rec.status == RunStatus.COMPLETED, rec.error_message
        ev = starts_of(events, "t1")[0]
        assert ev["resolved_model"] == "m1"       # 显式模型保留
        assert ev["resolved_effort"] == "low"     # effort 按 AUTO 回退
        assert "回退" in ev["reason"]              # 附理由


# ---------------------------------------------------------------------------
# 运行时变量插值失败：发 warning 事件（含引用名），不把 {{...}} 拼进 prompt
# ---------------------------------------------------------------------------


def test_unknown_variable_runtime_warning_event():
    g = PipelineGraph(graph_id="g_var", name="var", nodes=[
        GraphNode(id="s", type=NodeType.START),
        GraphNode(id="t1", type=NodeType.TASK, goal="write summary",
                  prompt="参考 {{ghost.output}} 来写", auto_split=False),
        GraphNode(id="e", type=NodeType.END),
    ], edges=[
        GraphEdge(id="e1", from_node="s", to_node="t1"),
        GraphEdge(id="e2", from_node="t1", to_node="e"),
    ])
    factory = E2EFactory(delay=0)
    with tempfile.TemporaryDirectory() as tmp:
        rec, events, _ = run_with_store(g, factory, tmp, "run_var")
        assert rec.status == RunStatus.COMPLETED, rec.error_message
        # 保存期 warning（指明引用名）
        result = PipelineCompiler.validate(g)
        assert any("ghost.output" in w for w in result.warnings)
        # 运行时 warning 事件（含 ref），且 prompt 不含原样 {{...}}
        warns = [ev for ev in events if ev["type"] == "warning"
                 and ev.get("ref") == "ghost.output"]
        assert warns and warns[0]["node_id"] == "t1"
        task_calls = [c for c in factory.calls if "write summary" in c["prompt"]]
        assert task_calls and "{{" not in task_calls[0]["prompt"]


# ---------------------------------------------------------------------------
# 辅助调用（拆解规划 / 格式化收尾 / goal 核验）均为轻档且落 events.jsonl
# ---------------------------------------------------------------------------


class AuxFactory:
    """按 prompt 内容路由：规划返回步骤数组、收尾返回 JSON、核验 PASS。"""

    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, prompt, schema=None, model=None, effort=None,
                       isolation=None, on_event=None):
        self.calls.append({"prompt": prompt, "model": model, "effort": effort})
        usage = {"input_tokens": 1, "output_tokens": 1}
        if "任务规划器" in prompt:
            return '["第一步", "第二步"]', usage
        if "只输出一个 JSON 对象" in prompt:
            return '{"path": "C:/out.json"}', usage
        if "验收标准" in prompt:
            return "PASS 达标", usage
        return "叙事输出没有 JSON", usage


def test_aux_calls_are_light_in_events():
    g = PipelineGraph(graph_id="g_aux", name="aux", nodes=[
        GraphNode(id="s", type=NodeType.START),
        GraphNode(id="tp", type=NodeType.TASK, goal="big split job",
                  model="m-explicit", auto_split=True),
        GraphNode(id="tf", type=NodeType.TASK, goal="structured job",
                  model="m-explicit", auto_split=False,
                  output_schema={"path": "string"}),
        GraphNode(id="g1", type=NodeType.GOAL, criteria="齐全吗"),
        GraphNode(id="e", type=NodeType.END),
    ], edges=[
        GraphEdge(id="e1", from_node="s", to_node="tp"),
        GraphEdge(id="e2", from_node="tp", to_node="tf"),
        GraphEdge(id="e3", from_node="tf", to_node="g1"),
        GraphEdge(id="e4", from_node="g1", to_node="e"),
    ])
    factory = AuxFactory()
    with tempfile.TemporaryDirectory() as tmp:
        rec, events, _ = run_with_store(g, factory, tmp, "run_aux")
        assert rec.status == RunStatus.COMPLETED, rec.error_message

        aux = {}
        for ev in events:
            if ev["type"] == "aux_call":
                aux.setdefault(ev["kind"], ev)
        # 拆解规划 / 格式化收尾：aux_call 事件为轻档 + low
        for kind in ("plan_split", "format_finish"):
            assert kind in aux, f"缺少 aux_call kind={kind}"
            assert aux[kind]["model"] == "light-x"
            assert aux[kind]["effort"] == "low"
        # goal 核验：node_start 的 resolved 决策为轻档 + low
        g1 = starts_of(events, "g1")[0]
        assert g1["resolved_model"] == "light-x"
        assert g1["resolved_effort"] == "low"


# ---------------------------------------------------------------------------
# API 全链路（TestClient）：注册 → 内联试跑 → 轮询完成 → events.jsonl
# ---------------------------------------------------------------------------


async def _caps():
    lv = ["off", "low", "medium", "high"]
    return {
        "models": [
            {"id": "explicit-m", "efforts": lv},
            {"id": "graph-m", "efforts": lv},
            {"id": "heavy-x", "efforts": lv},
            {"id": "light-x", "efforts": lv},
        ],
        "tools": ["Bash", "ReadFile"],
        "skills": ["demo-skill"],
        "modes": ["react", "plan", "go"],
    }


def _make_app(tmp, factory, caps=_caps, config=TIER_CFG):
    """注册 pipeline API；返回 (app, 恢复函数)。

    _load_app_config 被替换为固定 config，隔离宿主机的 .mewcode/config.yaml。
    """
    from fastapi import FastAPI

    import mewcode.pipeline.api as papi

    orig = papi._load_app_config
    papi._load_app_config = lambda: config
    app = FastAPI()
    papi.register_pipeline_api(
        app, tmp, agent_factory=factory, capabilities_provider=caps
    )

    def restore():
        papi._load_app_config = orig

    return app, restore


def _wait_run(client, graph_id, run_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/pipelines/{graph_id}/runs/{run_id}")
        if r.status_code == 200 and r.json()["status"] in (
            "completed", "failed", "interrupted"
        ):
            return r.json()
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} 未在 {timeout}s 内结束")


def test_e2e_api_inline_run_full_stack():
    from starlette.testclient import TestClient

    factory = E2EFactory(judge_tier="heavy")
    with tempfile.TemporaryDirectory() as tmp:
        app, restore = _make_app(tmp, factory)
        try:
            with TestClient(app) as client:
                graph_json = e2e_graph().model_dump(mode="json", by_alias=True)
                r = client.post("/api/pipelines/run", json={
                    "graph": graph_json,
                    "trigger_input": {"note": "hello-e2e"},
                })
                assert r.status_code == 200, r.text
                run_id = r.json()["run_id"]

                rec = _wait_run(client, "g_e2e_v02", run_id)
                assert rec["status"] == "completed", rec.get("error_message")
                # 提交的内容出现在运行记录的 trigger_input 里
                assert rec["trigger_input"] == {"note": "hello-e2e"}

                # events.jsonl 由 API 层自动落盘，结构断言同 executor 级
                store = PipelineStore(tmp)
                events = store.load_run_events(run_id)
                path = (Path(tmp) / ".mewcode/pipelines/runs"
                        / f"{run_id}.events.jsonl")
                assert_common_e2e_shape(events, path)
                assert starts_of(events, "tA")[0]["source"] == "node"
                assert starts_of(events, "tC")[0]["source"] == "auto-llm"

                # 已结束 run 的事件订阅端点可回放（含 node_start）
                r2 = client.get(
                    f"/api/pipelines/g_e2e_v02/runs/{run_id}/events"
                )
                assert r2.status_code == 200
                assert "node_start" in r2.text
        finally:
            restore()


# ---------------------------------------------------------------------------
# 兼容验收：v0.1 形态 JSON 直接加载 / 校验 / 编译 / 编辑 / 运行
# ---------------------------------------------------------------------------

V01_RAW = {
    "graph_id": "g_v01_legacy",
    "name": "v01-legacy",
    "description": "v0.1 时期保存的旧图（无 model/effort/default_* 字段）",
    "version": 3,
    "nodes": [
        {"id": "s", "type": "start", "label": "开始"},
        {"id": "t1", "type": "task", "title": "旧任务", "goal": "do legacy work"},
        {"id": "c1", "type": "condition", "condition_mode": "judgment",
         "ref_task": "t1", "criteria": "artifact ok?"},
        {"id": "t2", "type": "task", "goal": "after yes"},
        {"id": "e", "type": "end", "label": "结束"},
    ],
    "edges": [
        {"id": "e1", "from": "s", "to": "t1"},
        {"id": "e2", "from": "t1", "to": "c1"},
        {"id": "e3", "from": "c1", "to": "t2", "label": "yes"},
        {"id": "e4", "from": "c1", "to": "t1", "label": "no"},
        {"id": "e5", "from": "t2", "to": "e"},
    ],
    "variables": {},
    "created_at": "2025-11-01T00:00:00+00:00",
    "updated_at": "2025-11-01T00:00:00+00:00",
}


def test_v01_legacy_graph_load_validate_compile_run():
    from starlette.testclient import TestClient

    factory = E2EFactory(fail_times=0, delay=0)   # judgment 直接 PASS
    with tempfile.TemporaryDirectory() as tmp:
        # 模拟 v0.1 时期落盘的文件（原样字节，不经过新模型序列化）
        pdir = Path(tmp) / ".mewcode/pipelines"
        pdir.mkdir(parents=True)
        (pdir / "g_v01_legacy.json").write_text(
            json.dumps(V01_RAW, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # store 直接加载：新字段取默认值，无需迁移
        store = PipelineStore(tmp)
        graph = store.get_pipeline("g_v01_legacy")
        assert graph is not None
        assert graph.default_model is None and graph.default_effort is None
        assert all(n.model is None and n.effort is None for n in graph.nodes)

        # validate 无 error；compile 可过（no 回边识别为循环）
        result = PipelineCompiler.validate(graph)
        assert result.valid, result.errors
        plan = PipelineCompiler.compile(graph)
        assert "e4" in plan.back_edges

        app, restore = _make_app(tmp, factory)
        try:
            with TestClient(app) as client:
                # 不做迁移直接编辑（PUT 带旧版本号走乐观锁）
                r = client.put("/api/pipelines/g_v01_legacy", json={
                    "description": "edited in v0.2", "version": 3,
                })
                assert r.status_code == 200, r.text
                assert r.json()["version"] == 4

                # 直接运行成功
                r = client.post("/api/pipelines/g_v01_legacy/run",
                                json={"trigger_input": {"src": "legacy"}})
                assert r.status_code == 200, r.text
                run_id = r.json()["run_id"]
                rec = _wait_run(client, "g_v01_legacy", run_id)
                assert rec["status"] == "completed", rec.get("error_message")

                # 空值按 AUTO 解析（配置档位 → 轻档 light-x，source=auto-*）
                events = store.load_run_events(run_id)
                t1 = starts_of(events, "t1")[0]
                assert t1["resolved_model"] == "light-x"
                assert t1["source"].startswith("auto")
        finally:
            restore()


# ---------------------------------------------------------------------------
# 端点冒烟：capabilities / validate / import / export / duplicate / PUT 409
# ---------------------------------------------------------------------------


def _simple_graph_json(name="smoke", node_effort=None, node_prompt=""):
    g = {
        "name": name,
        "nodes": [
            {"id": "s", "type": "start"},
            {"id": "t1", "type": "task", "goal": "do smoke",
             "prompt": node_prompt, "auto_split": False},
            {"id": "e", "type": "end"},
        ],
        "edges": [
            {"id": "e1", "from": "s", "to": "t1"},
            {"id": "e2", "from": "t1", "to": "e"},
        ],
    }
    if node_effort is not None:
        g["nodes"][1]["effort"] = node_effort
    return g


def test_api_endpoint_smoke():
    from starlette.testclient import TestClient

    factory = E2EFactory(delay=0)
    with tempfile.TemporaryDirectory() as tmp:
        app, restore = _make_app(tmp, factory)
        try:
            with TestClient(app) as client:
                # capabilities：五键契约 + models 各项带 efforts
                r = client.get("/api/pipelines/capabilities")
                assert r.status_code == 200
                caps = r.json()
                assert set(caps) == {
                    "models", "tools", "skills", "actions", "modes",
                }
                assert caps["actions"] == []
                assert caps["modes"] == ["react", "plan", "go"]
                assert caps["models"] and all(
                    isinstance(m["efforts"], list) for m in caps["models"]
                )

                # 保存非法 effort：保存成功但 warning 含节点 id
                r = client.post("/api/pipelines",
                                json=_simple_graph_json(node_effort="ultra"))
                assert r.status_code == 200, r.text
                body = r.json()
                assert any("t1" in w and "ultra" in w for w in body["warnings"])
                gid = body["graph_id"]

                # 免保存校验：condition 缺 no 出边 → error 含节点 id 与标签名
                bad = {
                    "name": "bad-cond",
                    "nodes": [
                        {"id": "s", "type": "start"},
                        {"id": "t1", "type": "task", "goal": "g",
                         "auto_split": False},
                        {"id": "c9", "type": "condition",
                         "condition_mode": "judgment",
                         "ref_task": "t1", "criteria": "ok?"},
                        {"id": "e", "type": "end"},
                    ],
                    "edges": [
                        {"id": "e1", "from": "s", "to": "t1"},
                        {"id": "e2", "from": "t1", "to": "c9"},
                        {"id": "e3", "from": "c9", "to": "e", "label": "yes"},
                    ],
                }
                r = client.post("/api/pipelines/validate", json=bad)
                assert r.status_code == 200
                v = r.json()
                assert v["valid"] is False
                assert any("c9" in e and "no" in e for e in v["errors"])

                # 免保存校验：未知变量引用 → warning 指明引用名
                r = client.post(
                    "/api/pipelines/validate",
                    json=_simple_graph_json(node_prompt="用 {{ghost.output}}"),
                )
                assert r.status_code == 200
                assert any("ghost.output" in w for w in r.json()["warnings"])

                # export → import：得到节点/连线等价的新管线
                r = client.get(f"/api/pipelines/{gid}/export")
                assert r.status_code == 200
                assert "attachment" in r.headers.get("content-disposition", "")
                exported = r.json()
                r = client.post("/api/pipelines/import", json=exported)
                assert r.status_code == 200, r.text
                imported = r.json()
                assert imported["graph_id"] != gid
                assert [n["id"] for n in imported["nodes"]] == \
                    [n["id"] for n in exported["nodes"]]
                assert [(e["from"], e["to"], e["label"])
                        for e in imported["edges"]] == \
                    [(e["from"], e["to"], e["label"])
                     for e in exported["edges"]]

                # duplicate：名称带「副本」后缀
                r = client.post(f"/api/pipelines/{gid}/duplicate")
                assert r.status_code == 200
                assert "副本" in r.json()["name"]

                # PUT 乐观锁：旧版本号保存 → 409 含 detail/current_version
                r = client.put(f"/api/pipelines/{gid}",
                               json={"description": "first", "version": 1})
                assert r.status_code == 200
                assert r.json()["version"] == 2
                r = client.put(f"/api/pipelines/{gid}",
                               json={"description": "stale", "version": 1})
                assert r.status_code == 409
                conflict = r.json()
                assert "detail" in conflict
                assert conflict["current_version"] == 2

                # DELETE
                r = client.delete(f"/api/pipelines/{gid}")
                assert r.status_code == 200 and r.json()["success"] is True
                assert client.get(f"/api/pipelines/{gid}").status_code == 404
        finally:
            restore()


# ---------------------------------------------------------------------------
# 协议档位 / 内置分档表 / config.yaml pipeline 节（checklist 机械项）
# ---------------------------------------------------------------------------


def test_effort_levels_by_protocol():
    # anthropic：任一模型 off/low/medium/high
    assert model_effort_levels("anthropic", "claude-sonnet-4") == \
        ["off", "low", "medium", "high"]
    # openai 非推理模型（名字不含 gpt-5/codex 且不以 o1/o3/o4 开头）→ 空
    assert model_effort_levels("openai", "gpt-4o") == []
    # openai 推理模型族 → low/medium/high
    assert model_effort_levels("openai", "gpt-5.2") == ["low", "medium", "high"]
    assert model_effort_levels("openai", "gpt-5.6-sol") == [
        "none", "low", "medium", "high", "xhigh", "max"
    ]
    assert model_effort_levels("openai", "o3-mini") == ["low", "medium", "high"]
    assert model_effort_levels("openai-compat", "codex-mini") == \
        ["low", "medium", "high"]


def test_builtin_model_tier_table():
    assert lookup_model_tier("claude-opus-4-20250514") == "heavy"
    assert lookup_model_tier("claude-3-5-haiku-20241022") == "light"
    assert lookup_model_tier("some-unknown-model") is None


def test_config_yaml_pipeline_tier_models():
    yaml_text = (
        "providers:\n"
        "  - name: anthropic\n"
        "    protocol: anthropic\n"
        "    base_url: https://api.anthropic.com\n"
        "    model: test-model\n"
        "    api_key: \"k\"\n"
        "pipeline:\n"
        "  auto_heavy_model: heavy-from-yaml\n"
        "  auto_light_model: light-from-yaml\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "config.yaml"
        p.write_text(yaml_text, encoding="utf-8")
        cfg = load_config(p)
        assert cfg.pipeline.auto_heavy_model == "heavy-from-yaml"
        assert cfg.pipeline.auto_light_model == "light-from-yaml"


# ---------------------------------------------------------------------------
# main：无 pytest 时直接运行
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import io
    import traceback

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
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
