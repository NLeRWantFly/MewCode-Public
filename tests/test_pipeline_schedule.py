# -*- coding: utf-8 -*-
"""Pipeline 定时触发（SCHEDULE 节点）P0 回归。

覆盖：
1. compiler 校验：schedule 配置缺失/非法 cron → error，入边可作为流程入口；
   仅 schedule_text 未解析 → warning；合法图可编译
2. executor 入口参数化：整图 run 到 schedule 只布防、不穿透；
   entry_node_id 从 schedule/中间节点向后按序执行子图；非法入口 FAILED
3. PipelineScheduleStore：sync diff（注册/保留统计/清理）、
   get_due 到期口径（不立即误触发、disabled 不触发）
4. PipelineScheduler.check_and_fire：到期回调、回调异常不中断且不重复触发
5. schedule_resolver 规则层：中英文常见排期 → cron、cron 透传、
   不可解析返回 None；LLM 回复解析
6. 本地时区语义：compute_next_fire 的本地墙钟口径
7. API：保存时规则自动解析注册、preview 端点（规则/LLM 兜底/不可解析）

不依赖真实 LLM。运行：pytest tests/test_pipeline_schedule.py -q
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mewcode.config import PipelineConfig
from mewcode.pipeline.compiler import PipelineCompiler
from mewcode.pipeline.executor import PipelineExecutor
from mewcode.pipeline.models import (
    GraphEdge,
    GraphNode,
    NodeStatus,
    NodeType,
    PipelineGraph,
    PipelineRunRecord,
    RunStatus,
    ScheduleConfig,
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
    SOURCE_CRON,
    SOURCE_INTERVAL,
    SOURCE_LLM,
    SOURCE_ONCE,
    SOURCE_RULE,
    parse_llm_reply,
    resolve_schedule_text,
)
from mewcode.pipeline.next_run import decide_by_rule, parse_next_run_reply
from mewcode.pipeline.schedule_review import (
    ScheduleReviewDecision,
    build_schedule_review_prompt,
    parse_schedule_review_reply,
    reconcile_ephemeral_review,
    structural_review,
)

TIER_CFG = PipelineConfig(auto_heavy_model="heavy-x", auto_light_model="light-x")


class StubFactory:
    """最小 mock agent：记录调用并立即返回 ok。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, prompt, schema=None, model=None, effort=None,
                       isolation=None, on_event=None):
        self.calls.append({"prompt": prompt, "model": model})
        if "定时任务的独立审查员" in prompt:
            return '{"passed":true,"reason":"目标与交付结果一致"}', {
                "input_tokens": 1,
                "output_tokens": 1,
            }
        return "ok", {"input_tokens": 1, "output_tokens": 1}


def sched_graph() -> PipelineGraph:
    """主链 s→A→e 与定时关卡 s→sch→B→C→e2 并存的图。"""
    return PipelineGraph(
        graph_id="g_sched", name="sched-p0",
        nodes=[
            GraphNode(id="s", type=NodeType.START),
            GraphNode(id="A", type=NodeType.TASK, title="A",
                      goal="main chain work", auto_split=False),
            GraphNode(id="e", type=NodeType.END),
            GraphNode(id="sch", type=NodeType.SCHEDULE,
                      schedule=ScheduleConfig(
                          schedule_text="每天10点",
                          resolved_cron="0 10 * * *",
                          review_enabled=False,
                      )),
            GraphNode(id="B", type=NodeType.TASK, title="B",
                      goal="experiment step one", auto_split=False),
            GraphNode(id="C", type=NodeType.TASK, title="C",
                      goal="experiment step two", auto_split=False),
            GraphNode(id="e2", type=NodeType.END, label="exp-done"),
        ],
        edges=[
            GraphEdge(id="e_sa", from_node="s", to_node="A"),
            GraphEdge(id="e_ae", from_node="A", to_node="e"),
            GraphEdge(id="e_ss", from_node="s", to_node="sch"),
            GraphEdge(id="e_tb", from_node="sch", to_node="B"),
            GraphEdge(id="e_bc", from_node="B", to_node="C"),
            GraphEdge(id="e_ce", from_node="C", to_node="e2"),
        ],
    )


def run_graph(graph, factory, *, entry=None, trigger_input=None):
    """跑一次并返回 (record, 执行顺序)。"""
    order: list[str] = []
    executor = PipelineExecutor(
        agent_factory=factory,
        config=TIER_CFG,
        on_node_start=lambda nid, ntype: order.append(nid),
    )
    record = asyncio.run(executor.execute(
        graph, trigger_input, entry_node_id=entry,
    ))
    return record, order


# ---------------------------------------------------------------------------
# 1. compiler 校验
# ---------------------------------------------------------------------------


def test_validate_schedule_ok():
    result = PipelineCompiler.validate(sched_graph())
    assert result.valid, result.errors
    # 合法 schedule 不产生 schedule 相关告警
    assert not any("Schedule" in w for w in result.warnings)


def test_schedule_config_enables_review_by_default():
    assert ScheduleConfig().review_enabled is True


def test_validate_schedule_missing_config():
    g = sched_graph()
    g.get_node("sch").schedule = None
    result = PipelineCompiler.validate(g)
    assert not result.valid
    assert any("missing schedule config" in e for e in result.errors)


def test_validate_schedule_bad_cron():
    g = sched_graph()
    g.get_node("sch").schedule.resolved_cron = "not a cron"
    result = PipelineCompiler.validate(g)
    assert not result.valid
    assert any("invalid cron" in e for e in result.errors)


def test_validate_schedule_text_only_warns():
    g = sched_graph()
    g.get_node("sch").schedule.resolved_cron = ""
    result = PipelineCompiler.validate(g)
    assert result.valid, result.errors
    assert any("no resolved schedule" in w for w in result.warnings)


def test_validate_schedule_accepts_incoming_edge():
    g = sched_graph()
    g.edges.append(GraphEdge(id="e_as", from_node="A", to_node="sch"))
    result = PipelineCompiler.validate(g)
    assert result.valid, result.errors


def test_validate_schedule_no_incoming_warns():
    g = sched_graph()
    g.edges = [ed for ed in g.edges if ed.to_node != "sch"]
    result = PipelineCompiler.validate(g)
    assert result.valid, result.errors
    assert any("no incoming edge" in w for w in result.warnings)


def test_validate_schedule_no_outgoing_warns():
    g = sched_graph()
    g.edges = [ed for ed in g.edges if ed.from_node != "sch"]
    result = PipelineCompiler.validate(g)
    assert result.valid, result.errors
    assert any("no outgoing edges" in w for w in result.warnings)


def test_validate_adaptive_schedule_requires_policy():
    g = sched_graph()
    g.get_node("sch").schedule.adaptive = True
    result = PipelineCompiler.validate(g)
    assert not result.valid
    assert any("adaptive_policy" in e for e in result.errors)


def test_compile_with_schedule_gate():
    plan = PipelineCompiler.compile(sched_graph())
    assert set(plan.node_map) == {"s", "A", "e", "sch", "B", "C", "e2"}


# ---------------------------------------------------------------------------
# 2. executor 入口参数化
# ---------------------------------------------------------------------------


def test_full_run_arms_schedule_without_entering_downstream():
    record, order = run_graph(sched_graph(), StubFactory())
    assert record.status == RunStatus.COMPLETED
    assert {"s", "A", "e", "sch"}.issubset(order)
    assert record.context_outputs["sch"]["status"] == "armed"
    # 普通流程只在左侧入口布防，不会越过定时关卡执行右侧任务
    assert not {"B", "C", "e2"} & set(order)


def test_entry_from_schedule_runs_subgraph_in_order():
    trigger = {"trigger": {"type": "schedule", "run_index": 3}}
    record, order = run_graph(
        sched_graph(), StubFactory(), entry="sch", trigger_input=trigger,
    )
    assert record.status == RunStatus.COMPLETED
    assert record.entry_node_id == "sch"
    # 只执行定时支路，且从前往后按序
    assert order == ["sch", "B", "C", "e2"]
    assert record.context_outputs["sch"]["status"] == "triggered"
    assert record.node_records["B"].status == NodeStatus.COMPLETED
    assert record.node_records["C"].status == NodeStatus.COMPLETED
    # 主链不执行
    assert "A" not in record.node_records
    # 触发上下文保留在 run 记录
    assert record.trigger_input["trigger"]["run_index"] == 3


def test_entry_from_mid_node():
    record, order = run_graph(sched_graph(), StubFactory(), entry="A")
    assert record.status == RunStatus.COMPLETED
    assert order == ["A", "e"]
    assert "s" not in record.node_records


def test_entry_unknown_node_fails():
    record, _ = run_graph(sched_graph(), StubFactory(), entry="nope")
    assert record.status == RunStatus.FAILED
    assert "Entry node" in (record.error_message or "")


def test_resume_run_preserves_schedule_entry(monkeypatch):
    import mewcode.pipeline.api as papi

    graph = sched_graph()
    source = PipelineRunRecord(
        run_id="run_interrupted",
        graph_id=graph.graph_id,
        graph_name=graph.name,
        status=RunStatus.INTERRUPTED,
        entry_node_id="sch",
        trigger_input={"trigger": {"type": "schedule", "node_id": "sch"}},
    )
    captured: dict = {}

    class GraphStore:
        def find_run(self, graph_id, run_id):
            return source

        def get_pipeline(self, graph_id):
            return graph

    def fake_start(graph_arg, trigger_input, **kwargs):
        captured.update(kwargs)
        return papi._RunHandle("run_resumed", graph_arg.graph_id)

    monkeypatch.setattr(papi, "_store", GraphStore())
    monkeypatch.setattr(papi, "_active_runs", {})
    monkeypatch.setattr(papi, "_start_run", fake_start)

    asyncio.run(papi.resume_run(graph.graph_id, source.run_id))
    assert captured["resume_from"] is source
    assert captured["entry_node_id"] == "sch"


# ---------------------------------------------------------------------------
# 3. PipelineScheduleStore
# ---------------------------------------------------------------------------


def _iso_ago(minutes: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(minutes=minutes)
    ).isoformat()


def test_sync_graph_registers_and_diffs(tmp_path):
    store = PipelineScheduleStore(str(tmp_path))
    g = sched_graph()
    triggers = store.sync_graph(g)
    assert [t.node_id for t in triggers] == ["sch"]
    assert triggers[0].cron == "0 10 * * *"

    # 保留触发统计
    store.mark_fired(g.graph_id, "sch")
    assert store.get(g.graph_id, "sch").run_count == 1
    store.sync_graph(g)
    assert store.get(g.graph_id, "sch").run_count == 1

    # 未解析 cron 的节点不注册
    g.get_node("sch").schedule.resolved_cron = ""
    assert store.sync_graph(g) == []
    assert store.list(g.graph_id) == []


def test_sync_graph_persists_across_instances(tmp_path):
    g = sched_graph()
    PipelineScheduleStore(str(tmp_path)).sync_graph(g)
    reloaded = PipelineScheduleStore(str(tmp_path))
    assert [t.node_id for t in reloaded.list(g.graph_id)] == ["sch"]


def test_remove_graph(tmp_path):
    store = PipelineScheduleStore(str(tmp_path))
    g = sched_graph()
    store.sync_graph(g)
    store.remove_graph(g.graph_id)
    assert store.list() == []


def test_get_due_semantics(tmp_path):
    store = PipelineScheduleStore(str(tmp_path))
    g = sched_graph()
    g.get_node("sch").schedule.resolved_cron = "* * * * *"
    store.sync_graph(g)
    t = store.get(g.graph_id, "sch")

    # 新注册（无 last_fired_at）：以 now 为基点算下个整分 → 不立即触发
    assert store.get_due() == []

    # 上次触发在 2 分钟前 → 到期
    t.last_fired_at = _iso_ago(2)
    assert [d.node_id for d in store.get_due()] == ["sch"]

    # disabled 不触发
    t.enabled = False
    assert store.get_due() == []


def test_adaptive_trigger_is_one_shot_and_persists_next_fire(tmp_path):
    store = PipelineScheduleStore(str(tmp_path))
    g = sched_graph()
    cfg = g.get_node("sch").schedule
    cfg.resolved_cron = "* * * * *"
    cfg.adaptive = True
    cfg.adaptive_policy = "失败半小时后重试，成功则明天10点再跑"
    store.sync_graph(g)
    trigger = store.get(g.graph_id, "sch")
    assert trigger.adaptive is True
    assert trigger.next_fire_at

    trigger.next_fire_at = _iso_ago(1)
    assert store.get_due()
    store.mark_fired(g.graph_id, "sch")
    assert store.get_due() == []
    assert trigger.next_fire_at == ""

    # 保存图不能在本轮尚未完成时重新装回首次排期
    store.sync_graph(g)
    assert store.get(g.graph_id, "sch").next_fire_at == ""

    # 进程重启时则需要恢复，避免在 fire 与续约之间宕机导致永久断链
    assert store.recover_pending_adaptive() == 1
    assert store.get(g.graph_id, "sch").next_fire_at

    next_fire = datetime.now(timezone.utc) + timedelta(hours=2)
    assert store.schedule_next(g.graph_id, "sch", next_fire, "result-aware retry")
    assert store.get(g.graph_id, "sch").next_fire_reason == "result-aware retry"
    reloaded = PipelineScheduleStore(str(tmp_path))
    assert reloaded.get(g.graph_id, "sch").next_fire_at


def test_absolute_datetime_trigger_fires_only_once(tmp_path):
    store = PipelineScheduleStore(str(tmp_path))
    g = sched_graph()
    cfg = g.get_node("sch").schedule
    cfg.resolved_cron = ""
    cfg.resolved_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    trigger = store.sync_graph(g)[0]
    assert trigger.run_at == cfg.resolved_at
    assert store.get_due()

    store.mark_fired(g.graph_id, "sch")
    assert store.get_due() == []
    assert trigger.one_shot_completed is True
    store.sync_graph(g)
    assert store.get(g.graph_id, "sch").one_shot_completed is True
    assert store.get_due() == []


def test_finite_interval_trigger_stops_at_max_runs(tmp_path):
    store = PipelineScheduleStore(str(tmp_path))
    g = sched_graph()
    cfg = g.get_node("sch").schedule
    cfg.resolved_cron = ""
    cfg.resolved_at = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    cfg.resolved_interval_seconds = 10
    cfg.resolved_max_runs = 5
    trigger = store.sync_graph(g)[0]

    for expected_count in range(1, 6):
        next_fire = trigger.next_fire()
        assert next_fire is not None
        assert store.get_due(now=next_fire)
        store.mark_fired(g.graph_id, "sch")
        assert trigger.run_count == expected_count

    assert trigger.next_fire() is None
    assert store.get_due(now=datetime.now(timezone.utc) + timedelta(days=1)) == []

    reloaded = PipelineScheduleStore(str(tmp_path))
    saved = reloaded.get(g.graph_id, "sch")
    assert saved is not None
    assert saved.run_count == 5
    assert saved.max_runs == 5
    assert saved.next_fire() is None


def test_launch_retry_survives_graph_sync_and_restart(tmp_path):
    store = PipelineScheduleStore(str(tmp_path))
    graph = sched_graph()
    failed_at = datetime.now(timezone.utc)
    trigger = store.sync_graph(graph)[0]

    assert store.mark_launch_failed(
        graph.graph_id,
        "sch",
        "runner unavailable",
        failed_at=failed_at,
    )
    retry_at = failed_at + timedelta(seconds=2)
    assert trigger.next_fire() == retry_at

    store.sync_graph(graph)
    synced = store.get(graph.graph_id, "sch")
    assert synced.launch_retry_count == 1
    assert synced.next_fire() == retry_at

    reloaded = PipelineScheduleStore(str(tmp_path)).get(graph.graph_id, "sch")
    assert reloaded.launch_retry_count == 1
    assert reloaded.last_launch_error == "runner unavailable"
    assert reloaded.next_fire() == retry_at


def test_reviewed_interval_blocks_and_counts_only_passed_runs(tmp_path):
    store = PipelineScheduleStore(str(tmp_path))
    g = sched_graph()
    cfg = g.get_node("sch").schedule
    cfg.resolved_cron = ""
    cfg.resolved_at = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    cfg.resolved_interval_seconds = 10
    cfg.resolved_max_runs = 2
    cfg.review_enabled = True
    trigger = store.sync_graph(g)[0]

    assert store.get_due()
    assert store.mark_in_flight(g.graph_id, "sch", "run_1")
    store.mark_fired(g.graph_id, "sch")
    assert trigger.run_count == 0
    assert trigger.next_fire() is None
    assert trigger.in_flight_phase == "running"

    assert store.mark_reviewing(g.graph_id, "sch", "run_1")
    reviewed_at = datetime.now(timezone.utc)
    assert store.finish_review(
        g.graph_id,
        "sch",
        "run_1",
        passed=False,
        reason="没有产生可验证交付物",
        reviewed_at=reviewed_at,
    )
    assert trigger.run_count == 0
    assert trigger.last_review_status == "failed"
    assert trigger.next_fire() == reviewed_at + timedelta(seconds=10)
    assert store.get_due(reviewed_at + timedelta(seconds=9)) == []
    assert store.get_due(reviewed_at + timedelta(seconds=10)) == [trigger]

    assert store.mark_in_flight(g.graph_id, "sch", "run_2")
    assert store.finish_review(
        g.graph_id,
        "sch",
        "run_2",
        passed=True,
        reason="交付物已核实",
        reviewed_at=reviewed_at + timedelta(seconds=11),
    )
    assert trigger.run_count == 1
    assert trigger.attempt_count == 2

    reloaded = PipelineScheduleStore(str(tmp_path)).get(g.graph_id, "sch")
    assert reloaded is not None
    assert reloaded.run_count == 1
    assert reloaded.last_review_status == "passed"


def test_legacy_dispatch_count_is_not_migrated_as_reviewed_completion(tmp_path):
    schedules_file = tmp_path / ".mewcode" / "pipelines" / "schedules.json"
    schedules_file.parent.mkdir(parents=True)
    schedules_file.write_text(
        '[{"graph_id":"g_sched","node_id":"sch","cron":"0 10 * * *",'
        '"run_count":5,"last_fired_at":"2026-07-29T00:00:00+00:00"}]',
        encoding="utf-8",
    )
    store = PipelineScheduleStore(str(tmp_path))
    graph = sched_graph()
    graph.get_node("sch").schedule.review_enabled = True
    trigger = store.sync_graph(graph)[0]
    assert trigger.review_enabled is True
    assert trigger.run_count == 0
    assert trigger.last_fired_at == ""


def test_schedule_review_parser_and_structural_failure():
    passed = parse_schedule_review_reply(
        '说明 {"passed":true,"reason":"已核实通知送达"}'
    )
    assert passed is not None and passed.passed is True
    failed = parse_schedule_review_reply("FAIL 缺少实际输出")
    assert failed is not None and failed.passed is False

    record = PipelineRunRecord(
        graph_id="g",
        status=RunStatus.FAILED,
        error_message="task crashed",
    )
    decision = structural_review(record)
    assert decision is not None and decision.passed is False
    assert "task crashed" in decision.reason


def test_ephemeral_review_uses_dispatch_evidence_not_process_lifetime():
    criteria = "任务：发送你好通知，使用 Windows 弹窗。"
    summary = {
        "node_results": {
            "task": {
                "status": "completed",
                "result": "弹窗已显示，进程 ID 为 17056，命令执行正常。",
                "error": None,
            }
        }
    }
    prompt = build_schedule_review_prompt(criteria, summary)
    assert "不要求窗口、声音或临时进程在审查时仍然存在" in prompt

    rejected = ScheduleReviewDecision(
        False,
        "PID 17056 已结束，无法确认弹窗在审查时仍存在",
        "agent",
    )
    reconciled = reconcile_ephemeral_review(rejected, criteria, summary)
    assert reconciled.passed is True
    assert reconciled.source == "agent-ephemeral-evidence"


def test_ephemeral_review_does_not_hide_real_failures():
    rejected = ScheduleReviewDecision(False, "命令返回失败", "agent")
    summary = {"node_results": {"task": {"result": "发送通知失败"}}}
    assert reconcile_ephemeral_review(
        rejected, "发送 Windows 通知", summary
    ) == rejected


def test_scheduled_task_prompt_executes_side_effect_once_and_returns():
    graph = sched_graph()
    factory = StubFactory()
    record, _order = run_graph(
        graph,
        factory,
        entry="sch",
        trigger_input={
            "trigger": {
                "type": "schedule",
                "run_index": 2,
                "max_runs": 3,
            }
        },
    )
    assert record.status == RunStatus.COMPLETED
    task_prompts = [call["prompt"] for call in factory.calls]
    assert task_prompts
    # B、C 各一次：定时副作用绕过 auto_split，执行与凭据留在同一会话。
    assert len(task_prompts) == 2
    assert all("这是定时任务第 2/3 轮" in prompt for prompt in task_prompts)
    assert all("成功的目标动作只能发生一次" in prompt for prompt in task_prompts)
    assert all('"evidence"' in prompt for prompt in task_prompts)


# ---------------------------------------------------------------------------
# 4. PipelineScheduler
# ---------------------------------------------------------------------------


def _due_store(tmp_path) -> tuple[PipelineScheduleStore, PipelineGraph]:
    store = PipelineScheduleStore(str(tmp_path))
    g = sched_graph()
    g.get_node("sch").schedule.resolved_cron = "* * * * *"
    store.sync_graph(g)
    store.get(g.graph_id, "sch").last_fired_at = _iso_ago(2)
    return store, g


def test_check_and_fire_invokes_callback(tmp_path):
    store, g = _due_store(tmp_path)
    fired: list[PipelineTrigger] = []
    scheduler = PipelineScheduler(store, on_fire=fired.append)

    assert scheduler.check_and_fire() == 1
    assert len(fired) == 1
    assert fired[0].graph_id == g.graph_id and fired[0].node_id == "sch"
    # 触发统计更新
    assert store.get(g.graph_id, "sch").run_count == 1
    # 同一到期点不重复触发
    assert scheduler.check_and_fire() == 0


def test_check_and_fire_callback_error_swallowed(tmp_path):
    store, g = _due_store(tmp_path)

    def boom(trigger: PipelineTrigger) -> None:
        raise RuntimeError("boom")

    scheduler = PipelineScheduler(store, on_fire=boom)
    failed_at = datetime.now(timezone.utc)
    assert scheduler.check_and_fire(failed_at) == 0
    trigger = store.get(g.graph_id, "sch")
    # 没有生成真实 run：不消耗运行次数，持久化退避后重试。
    assert trigger.run_count == 0
    assert trigger.attempt_count == 0
    assert trigger.launch_retry_count == 1
    retry_at = datetime.fromisoformat(trigger.next_launch_at)
    assert retry_at == failed_at + timedelta(seconds=2)
    assert scheduler.check_and_fire(failed_at + timedelta(seconds=1)) == 0

    scheduler._on_fire = lambda _trigger: True
    assert scheduler.check_and_fire(retry_at) == 1
    assert trigger.run_count == 1
    assert trigger.launch_retry_count == 0
    assert trigger.next_launch_at == ""


def test_finite_interval_deferred_callback_does_not_consume_run(tmp_path):
    store = PipelineScheduleStore(str(tmp_path))
    g = sched_graph()
    cfg = g.get_node("sch").schedule
    cfg.resolved_cron = ""
    cfg.resolved_at = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    cfg.resolved_interval_seconds = 10
    cfg.resolved_max_runs = 5
    trigger = store.sync_graph(g)[0]
    accept = False

    def dispatch(_trigger):
        return accept

    scheduler = PipelineScheduler(store, on_fire=dispatch)
    due_at = datetime.now(timezone.utc)
    assert scheduler.check_and_fire(due_at) == 0
    assert trigger.run_count == 0
    assert store.get_due(due_at) == []
    retry_at = datetime.fromisoformat(trigger.next_launch_at)
    assert retry_at == due_at + timedelta(seconds=2)

    accept = True
    assert scheduler.check_and_fire(retry_at) == 1
    assert trigger.run_count == 1


def test_unreviewed_trigger_also_waits_for_active_run(tmp_path, monkeypatch):
    import mewcode.pipeline.api as papi

    graph = sched_graph()

    class GraphStore:
        def get_pipeline(self, graph_id):
            return graph if graph_id == graph.graph_id else None

    active = papi._RunHandle("run_active", graph.graph_id)
    active.entry_node_id = "sch"
    started = []
    schedule_store = PipelineScheduleStore(str(tmp_path))
    graph.get_node("sch").schedule.review_enabled = False
    trigger = schedule_store.sync_graph(graph)[0]

    old_store = papi._store
    old_schedule_store = papi._schedule_store
    old_runs = papi._active_runs
    papi._store = GraphStore()
    papi._schedule_store = schedule_store
    papi._active_runs = {active.run_id: active}
    monkeypatch.setattr(
        papi,
        "_start_run",
        lambda *args, **kwargs: started.append((args, kwargs)),
    )
    try:
        assert papi._fire_trigger(trigger) is False
        assert started == []
    finally:
        papi._store = old_store
        papi._schedule_store = old_schedule_store
        papi._active_runs = old_runs


def test_unreviewed_fire_counts_only_after_real_run_finishes(
    tmp_path, monkeypatch
):
    import mewcode.pipeline.api as papi

    graph = sched_graph()
    cfg = graph.get_node("sch").schedule
    cfg.resolved_cron = ""
    cfg.resolved_at = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    cfg.resolved_interval_seconds = 10
    cfg.resolved_max_runs = 2
    cfg.review_enabled = False
    store = PipelineScheduleStore(str(tmp_path))
    trigger = store.sync_graph(graph)[0]
    handle = papi._RunHandle("run_structural", graph.graph_id)
    handle.entry_node_id = "sch"

    class GraphStore:
        def get_pipeline(self, graph_id):
            return graph if graph_id == graph.graph_id else None

    monkeypatch.setattr(papi, "_start_run", lambda *args, **kwargs: handle)
    old_store = papi._store
    old_schedule_store = papi._schedule_store
    old_runs = papi._active_runs
    old_tasks = papi._adaptive_tasks
    papi._store = GraphStore()
    papi._schedule_store = store
    papi._active_runs = {}
    papi._adaptive_tasks = set()

    async def scenario():
        assert papi._fire_trigger(trigger) is True
        assert trigger.run_count == 0
        assert trigger.in_flight_run_id == handle.run_id

        handle.record = PipelineRunRecord(
            run_id=handle.run_id,
            graph_id=graph.graph_id,
            status=RunStatus.COMPLETED,
        )
        handle.finish()
        await asyncio.gather(*list(papi._adaptive_tasks))

    try:
        asyncio.run(scenario())
        assert trigger.run_count == 1
        assert trigger.in_flight_run_id == ""
        assert trigger.last_review_status == "passed"
    finally:
        papi._store = old_store
        papi._schedule_store = old_schedule_store
        papi._active_runs = old_runs
        papi._adaptive_tasks = old_tasks


def test_reviewed_fire_waits_for_handle_and_agent_before_counting(
    tmp_path, monkeypatch
):
    import mewcode.pipeline.api as papi

    graph = sched_graph()
    cfg = graph.get_node("sch").schedule
    cfg.resolved_cron = ""
    cfg.resolved_at = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    cfg.resolved_interval_seconds = 10
    cfg.resolved_max_runs = 2
    cfg.review_enabled = True
    store = PipelineScheduleStore(str(tmp_path))
    trigger = store.sync_graph(graph)[0]
    factory = StubFactory()
    handle = papi._RunHandle("run_reviewed", graph.graph_id)
    handle.entry_node_id = "sch"

    class GraphStore:
        def get_pipeline(self, graph_id):
            return graph if graph_id == graph.graph_id else None

    monkeypatch.setattr(papi, "_start_run", lambda *args, **kwargs: handle)
    old_store = papi._store
    old_schedule_store = papi._schedule_store
    old_runs = papi._active_runs
    old_tasks = papi._adaptive_tasks
    old_kwargs = dict(papi._executor_kwargs)
    old_caps = papi._capabilities_provider
    papi._store = GraphStore()
    papi._schedule_store = store
    papi._active_runs = {}
    papi._adaptive_tasks = set()
    papi._executor_kwargs.clear()
    papi._executor_kwargs.update(agent_factory=factory, config=TIER_CFG)
    papi._capabilities_provider = None

    async def scenario():
        scheduler = PipelineScheduler(store, on_fire=papi._fire_trigger)
        assert scheduler.check_and_fire() == 1
        assert trigger.in_flight_run_id == handle.run_id
        assert trigger.in_flight_phase == "running"
        assert trigger.run_count == 0
        assert trigger.next_fire() is None

        handle.record = PipelineRunRecord(
            run_id=handle.run_id,
            graph_id=graph.graph_id,
            status=RunStatus.COMPLETED,
            final_output="任务已完成并产生通知",
        )
        handle.finish()
        await asyncio.gather(*list(papi._adaptive_tasks))

    try:
        asyncio.run(scenario())
        assert trigger.in_flight_run_id == ""
        assert trigger.last_review_status == "passed"
        assert trigger.run_count == 1
        assert any("定时任务的独立审查员" in call["prompt"] for call in factory.calls)
    finally:
        papi._store = old_store
        papi._schedule_store = old_schedule_store
        papi._active_runs = old_runs
        papi._adaptive_tasks = old_tasks
        papi._executor_kwargs.clear()
        papi._executor_kwargs.update(old_kwargs)
        papi._capabilities_provider = old_caps


# ---------------------------------------------------------------------------
# 5. schedule_resolver 规则层
# ---------------------------------------------------------------------------


def test_resolver_rule_cases():
    cases = {
        "每天10点": "0 10 * * *",
        "每天早上9点半": "30 9 * * *",
        "每天下午3点": "0 15 * * *",
        "每天晚上十一点": "0 23 * * *",
        "工作日9点半": "30 9 * * 1-5",
        "周末上午10点": "0 10 * * 0,6",
        "每周一三五 10点": "0 10 * * 1,3,5",
        "每周日8点15分": "15 8 * * 0",
        "每月1号 8点": "0 8 1 * *",
        "每5分钟": "*/5 * * * *",
        "每分钟": "* * * * *",
        "每2小时": "0 */2 * * *",
        "每小时": "0 * * * *",
        "daily at 10:30": "30 10 * * *",
        "weekdays at 9:30": "30 9 * * 1-5",
        "every monday at 9:00": "0 9 * * 1",
        "every 2 hours": "0 */2 * * *",
    }
    for text, expect in cases.items():
        r = resolve_schedule_text(text)
        assert r is not None, f"{text!r} 未命中规则层"
        assert r.cron == expect, f"{text!r}: {r.cron} != {expect}"
        assert r.source == SOURCE_RULE
        assert r.summary  # 有人话回显


def test_resolver_cron_passthrough():
    r = resolve_schedule_text("0 10 * * 1-5")
    assert r is not None
    assert r.cron == "0 10 * * 1-5"
    assert r.source == SOURCE_CRON


def test_resolver_absolute_datetime_once():
    now = datetime(2026, 7, 28, 18, 0).astimezone()
    r = resolve_schedule_text("2026-07-28 19:30 执行一次", now=now)
    assert r is not None
    assert r.source == SOURCE_ONCE
    assert r.cron == ""
    assert r.run_at
    local = datetime.fromisoformat(r.run_at).astimezone()
    assert (local.year, local.month, local.day, local.hour, local.minute) == (
        2026, 7, 28, 19, 30,
    )


def test_resolver_finite_seconds_interval():
    local_tz = timezone(timedelta(hours=8))
    now = datetime(2026, 7, 29, 12, 0, tzinfo=local_tz)
    r = resolve_schedule_text(
        "从12:03分开始的每10s，总共触发5次",
        now=now,
    )
    assert r is not None
    assert r.source == SOURCE_INTERVAL
    assert r.cron == ""
    assert r.interval_seconds == 10
    assert r.max_runs == 5
    start = datetime.fromisoformat(r.run_at).astimezone(local_tz)
    assert (start.year, start.month, start.day, start.hour, start.minute) == (
        2026, 7, 29, 12, 3,
    )
    fires = compute_interval_fires(
        datetime.fromisoformat(r.run_at),
        r.interval_seconds,
        count=5,
        max_runs=r.max_runs,
    )
    assert len(fires) == 5
    assert all(
        fires[index + 1] - fires[index] == timedelta(seconds=10)
        for index in range(4)
    )


def test_resolver_clock_only_interval_rolls_to_tomorrow_when_time_passed():
    local_tz = timezone(timedelta(hours=8))
    now = datetime(2026, 7, 29, 12, 6, tzinfo=local_tz)
    r = resolve_schedule_text("从12:03开始每10秒，共5次", now=now)
    assert r is not None
    start = datetime.fromisoformat(r.run_at).astimezone(local_tz)
    assert start == datetime(2026, 7, 30, 12, 3, tzinfo=local_tz)


def test_resolver_unresolvable_returns_none():
    for text in (
        "",
        "实验结束后4小时再跑",
        "每天",           # 缺时刻不猜
        "工作日",         # 缺时刻不猜
        "不定期跑一下",
    ):
        assert resolve_schedule_text(text) is None, f"{text!r} 不应命中"


def test_parse_llm_reply():
    ok = parse_llm_reply('前置说明 {"cron": "0 22 * * *", "summary": "每天22点"}')
    assert ok is not None and ok.cron == "0 22 * * *" and ok.source == SOURCE_LLM
    # cron 为空 / 非法 / 非 JSON → None
    assert parse_llm_reply('{"cron": "", "summary": "无法解析"}') is None
    assert parse_llm_reply('{"cron": "not cron", "summary": "x"}') is None
    assert parse_llm_reply("随便说点什么") is None
    once = parse_llm_reply(
        '{"cron":"","run_at":"2026-07-29T19:30:00+08:00",'
        '"summary":"执行一次"}'
    )
    assert once is not None and once.run_at and once.source == SOURCE_LLM
    interval = parse_llm_reply(
        '{"type":"interval","cron":"","run_at":"2026-07-30T12:03:00+08:00",'
        '"interval_seconds":10,"max_runs":5,"summary":"每10秒，共5次"}'
    )
    assert interval is not None
    assert interval.source == SOURCE_LLM
    assert interval.interval_seconds == 10
    assert interval.max_runs == 5


def test_adaptive_rule_decider_uses_run_outcome():
    now = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)
    policy = "失败半小时后重试，成功则明天10点再跑"
    failed = decide_by_rule(policy, succeeded=False, now=now)
    assert failed is not None
    assert failed.next_fire_at == now + timedelta(minutes=30)

    succeeded = decide_by_rule(policy, succeeded=True, now=now)
    assert succeeded is not None
    assert succeeded.next_fire_at.astimezone().hour == 10


def test_adaptive_llm_reply_rejects_past_time():
    now = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)
    ok = parse_next_run_reply(
        '{"next_fire_at":"2026-07-28T04:00:00+00:00","reason":"wait"}',
        now=now,
    )
    assert ok is not None and ok.source == "llm"
    assert parse_next_run_reply(
        '{"next_fire_at":"2026-07-28T01:00:00+00:00","reason":"past"}',
        now=now,
    ) is None


def test_adaptive_run_completion_renews_trigger(tmp_path):
    import mewcode.pipeline.api as papi

    store = PipelineScheduleStore(str(tmp_path))
    g = sched_graph()
    cfg = g.get_node("sch").schedule
    cfg.adaptive = True
    cfg.adaptive_policy = "失败半小时后重试，成功则明天10点再跑"
    store.sync_graph(g)
    trigger = store.get(g.graph_id, "sch")
    store.mark_fired(g.graph_id, "sch")

    handle = papi._RunHandle("run_adaptive", g.graph_id)
    handle.record = papi.PipelineRunRecord(
        run_id="run_adaptive",
        graph_id=g.graph_id,
        status=RunStatus.COMPLETED,
    )
    handle.finish()
    old_store = papi._schedule_store
    papi._schedule_store = store
    try:
        asyncio.run(papi._renew_adaptive_trigger(trigger, handle))
    finally:
        papi._schedule_store = old_store
    renewed = store.get(g.graph_id, "sch")
    assert renewed.next_fire_at
    assert "明天" in renewed.next_fire_reason


# ---------------------------------------------------------------------------
# 6. 本地时区语义
# ---------------------------------------------------------------------------


def test_next_fire_local_wall_clock():
    # 「每天10点」= 本地墙钟 10:00：换回本地时区后小时必须是 10
    nf = compute_next_fire("0 10 * * *")
    assert nf is not None
    local = nf.astimezone()
    assert (local.hour, local.minute) == (10, 0)
    # 返回值本身是 UTC aware
    assert nf.tzinfo == timezone.utc


def test_next_fires_sequence():
    fires = compute_next_fires("0 10 * * *", count=3)
    assert len(fires) == 3
    # 相邻两次触发相差 24 小时
    assert (fires[1] - fires[0]) == timedelta(days=1)
    assert (fires[2] - fires[1]) == timedelta(days=1)
    assert compute_next_fires("not cron", count=3) == []


# ---------------------------------------------------------------------------
# 7. API：保存自动解析 + preview
# ---------------------------------------------------------------------------


def _caps():
    async def caps():
        lv = ["off", "low", "medium", "high"]
        return {
            "models": [
                {"id": "heavy-x", "efforts": lv},
                {"id": "light-x", "efforts": lv},
            ],
            "tools": [], "skills": [], "modes": ["react", "plan", "go"],
        }
    return caps


def _make_app(tmp, factory):
    """注册 pipeline API（隔离宿主机 config）；返回 (app, 恢复函数)。"""
    from fastapi import FastAPI

    import mewcode.pipeline.api as papi

    orig = papi._load_app_config
    papi._load_app_config = lambda: TIER_CFG
    app = FastAPI()
    papi.register_pipeline_api(
        app, tmp, agent_factory=factory, capabilities_provider=_caps()
    )

    def restore():
        papi._load_app_config = orig

    return app, restore


class LlmResolveFactory(StubFactory):
    """排期解析 prompt 返回固定 JSON，其余走 StubFactory。"""

    def __init__(self, reply: str) -> None:
        super().__init__()
        self.reply = reply

    async def __call__(self, prompt, schema=None, model=None, effort=None,
                       isolation=None, on_event=None):
        self.calls.append({"prompt": prompt, "model": model})
        if "排期解析器" in prompt:
            return self.reply, {"input_tokens": 1, "output_tokens": 1}
        return "ok", {"input_tokens": 1, "output_tokens": 1}


def test_api_create_auto_resolves_schedule(tmp_path):
    from starlette.testclient import TestClient

    app, restore = _make_app(str(tmp_path), StubFactory())
    try:
        with TestClient(app) as client:
            g = sched_graph()
            g.get_node("sch").schedule.resolved_cron = ""  # 只有自然语言
            payload = g.model_dump(mode="json", by_alias=True)
            r = client.post("/api/pipelines", json={
                "name": payload["name"],
                "nodes": payload["nodes"],
                "edges": payload["edges"],
            })
            assert r.status_code == 200, r.text
            graph_id = r.json()["graph_id"]
            # 「每天10点」被规则层自动解析并注册
            r2 = client.get(f"/api/pipelines/{graph_id}/schedules")
            assert r2.status_code == 200
            schedules = r2.json()
            assert len(schedules) == 1
            assert schedules[0]["cron"] == "0 10 * * *"
            assert schedules[0]["next_fire"] is not None
    finally:
        restore()


def test_api_preview_rule_hit(tmp_path):
    from starlette.testclient import TestClient

    app, restore = _make_app(str(tmp_path), StubFactory())
    try:
        with TestClient(app) as client:
            r = client.post("/api/pipelines/schedules/preview",
                            json={"schedule_text": "工作日9点半"})
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["resolved"] is True
            assert data["cron"] == "30 9 * * 1-5"
            assert data["source"] == "rule"
            assert len(data["next_fires"]) == 3
    finally:
        restore()


def test_api_lifespan_starts_pipeline_scheduler(tmp_path):
    from starlette.testclient import TestClient
    import mewcode.pipeline.api as papi

    app, restore = _make_app(str(tmp_path), StubFactory())
    scheduler = papi._scheduler
    assert scheduler is not None and scheduler._running is False
    try:
        with TestClient(app):
            assert scheduler._running is True
        assert scheduler._running is False
    finally:
        restore()


def test_api_preview_absolute_datetime_once(tmp_path):
    from starlette.testclient import TestClient

    factory = StubFactory()
    app, restore = _make_app(str(tmp_path), factory)
    try:
        with TestClient(app) as client:
            future = datetime.now().astimezone() + timedelta(days=1)
            schedule_text = f"{future:%Y-%m-%d %H:%M} 执行一次"
            r = client.post(
                "/api/pipelines/schedules/preview",
                json={"schedule_text": schedule_text},
            )
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["resolved"] is True
            assert data["schedule_type"] == "once"
            assert data["cron"] == ""
            assert data["run_at"]
            assert len(data["next_fires"]) == 1
            assert data["source"] == "once"
            assert factory.calls == []
    finally:
        restore()


def test_api_preview_finite_seconds_interval(tmp_path):
    from starlette.testclient import TestClient

    factory = StubFactory()
    app, restore = _make_app(str(tmp_path), factory)
    try:
        with TestClient(app) as client:
            future = datetime.now().astimezone() + timedelta(days=1)
            text = (
                f"从{future:%Y-%m-%d %H:%M}开始，"
                "每10s，总共触发5次"
            )
            r = client.post(
                "/api/pipelines/schedules/preview",
                json={"schedule_text": text},
            )
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["resolved"] is True
            assert data["schedule_type"] == "interval"
            assert data["interval_seconds"] == 10
            assert data["max_runs"] == 5
            assert data["recommended_overlap_policy"] == "skip"
            assert data["timing_mode"] == "after_completion"
            assert "实际运行并审查结束后" in data["timing_note"]
            assert len(data["next_fires"]) == 1
            assert data["source"] == "interval"
            assert factory.calls == []
    finally:
        restore()


def test_api_preview_past_interval_starts_now_and_keeps_all_rounds(tmp_path):
    from starlette.testclient import TestClient

    past = datetime.now(timezone.utc) - timedelta(hours=2)
    factory = LlmResolveFactory(
        '{"type":"interval","cron":"","run_at":"'
        + past.isoformat()
        + '","interval_seconds":10,"max_runs":5,"summary":"继续跑五轮"}'
    )
    app, restore = _make_app(str(tmp_path), factory)
    try:
        with TestClient(app) as client:
            before = datetime.now(timezone.utc)
            response = client.post(
                "/api/pipelines/schedules/preview",
                json={"schedule_text": "机器恢复后继续把剩余轮次跑完"},
            )
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["resolved"] is True
            assert data["timing_mode"] == "after_completion"
            assert data["max_runs"] == 5
            assert len(data["next_fires"]) == 1
            first_fire = datetime.fromisoformat(data["next_fires"][0])
            assert first_fire >= before
    finally:
        restore()


def test_api_preview_llm_fallback(tmp_path):
    from starlette.testclient import TestClient

    factory = LlmResolveFactory(
        '{"cron": "0 22 * * *", "summary": "每天22点"}'
    )
    app, restore = _make_app(str(tmp_path), factory)
    try:
        with TestClient(app) as client:
            r = client.post("/api/pipelines/schedules/preview",
                            json={"schedule_text": "每逢佳节跑一次"})
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["resolved"] is True
            assert data["cron"] == "0 22 * * *"
            assert data["source"] == "llm"
            # 确实走了 LLM（prompt 含解析器指令）
            assert any("排期解析器" in c["prompt"] for c in factory.calls)
    finally:
        restore()


def test_api_save_gives_agent_chance_to_resolve_interval(tmp_path):
    from starlette.testclient import TestClient

    future = datetime.now(timezone.utc) + timedelta(days=1)
    factory = LlmResolveFactory(
        '{"type":"interval","cron":"","run_at":"'
        + future.isoformat()
        + '","interval_seconds":10,"max_runs":5,"summary":"五连发"}'
    )
    app, restore = _make_app(str(tmp_path), factory)
    try:
        with TestClient(app) as client:
            g = sched_graph()
            cfg = g.get_node("sch").schedule
            cfg.schedule_text = "进入窗口后短促跑五轮"
            cfg.resolved_cron = ""
            payload = g.model_dump(mode="json", by_alias=True)
            r = client.post("/api/pipelines", json={
                "name": payload["name"],
                "nodes": payload["nodes"],
                "edges": payload["edges"],
            })
            assert r.status_code == 200, r.text
            graph_id = r.json()["graph_id"]
            schedules = client.get(
                f"/api/pipelines/{graph_id}/schedules"
            ).json()
            assert len(schedules) == 1
            assert schedules[0]["interval_seconds"] == 10
            assert schedules[0]["max_runs"] == 5
            assert any("排期解析器" in call["prompt"] for call in factory.calls)
    finally:
        restore()


def test_api_preview_unresolvable(tmp_path):
    from starlette.testclient import TestClient

    factory = LlmResolveFactory('{"cron": "", "summary": "无固定周期"}')
    app, restore = _make_app(str(tmp_path), factory)
    try:
        with TestClient(app) as client:
            r = client.post("/api/pipelines/schedules/preview",
                            json={"schedule_text": "实验结束后4小时再跑"})
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["resolved"] is False
            assert data["reason"]
    finally:
        restore()


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
