# -*- coding: utf-8 -*-
"""Pipeline 稳定性回归测试。

用可编程的假 agent_factory 驱动 executor，覆盖：
- 瞬时错误重试 / 非瞬时错误不重试
- 空 str() 异常的错误保真（TimeoutError → 记录类型名）
- PASS/FAIL 裁决解析（「FAIL，因为不满足 PASS 条件」不再误判）
- 条件重试回边耗尽 → run 失败（不再静默成功）
- 断点续跑：已完成节点复用，失败节点重跑
- token 计量落库 + 预算拦截
- 头尾截断保留末尾的文件路径
- checkpoint 回调按节点粒度触发

不依赖真实 LLM，毫秒级跑完。运行方式二选一：
    pytest tests/test_pipeline_stability.py -q
    python tests/test_pipeline_stability.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mewcode.errors import format_error, is_transient_error
from mewcode.pipeline.executor import (
    BudgetExceededError,
    LoopExhaustedError,
    PipelineExecutor,
)
from mewcode.pipeline.models import (
    ConditionMode,
    GraphEdge,
    GraphNode,
    NodeStatus,
    NodeType,
    PipelineGraph,
    RunStatus,
)


# ---------------------------------------------------------------------------
# 假 agent factory
# ---------------------------------------------------------------------------


class FakeFactory:
    """可编程的 agent_factory。

    script 是一个列表：每次调用弹出一项。
    - 字符串 → 作为结果返回
    - 异常实例 → 抛出
    - 可调用 → fn(call_index, prompt) 返回结果或抛异常
    列表耗尽后重复最后一项。
    """

    def __init__(self, script, usage=None):
        self.script = list(script)
        self.calls = []
        self.usage = usage or {"input_tokens": 100, "output_tokens": 50}

    async def __call__(
        self, prompt, schema=None, model=None, effort=None,
        isolation=None, on_event=None,
    ):
        idx = len(self.calls)
        self.calls.append(prompt)
        item = self.script[idx] if idx < len(self.script) else self.script[-1]
        if callable(item) and not isinstance(item, BaseException):
            item = item(idx, prompt)
        if isinstance(item, BaseException):
            raise item
        return str(item), dict(self.usage)


def make_graph(nodes, edges):
    return PipelineGraph(graph_id="g_test", name="test", nodes=nodes, edges=edges)


def linear_graph(task_goal="do the thing", auto_split=False):
    """start → task → end"""
    return make_graph(
        nodes=[
            GraphNode(id="s", type=NodeType.START),
            GraphNode(id="t1", type=NodeType.TASK, goal=task_goal, auto_split=auto_split),
            GraphNode(id="e", type=NodeType.END),
        ],
        edges=[
            GraphEdge(id="e1", from_node="s", to_node="t1"),
            GraphEdge(id="e2", from_node="t1", to_node="e"),
        ],
    )


def two_task_graph():
    """start → A → B → end"""
    return make_graph(
        nodes=[
            GraphNode(id="s", type=NodeType.START),
            GraphNode(id="a", type=NodeType.TASK, goal="task A", auto_split=False),
            GraphNode(id="b", type=NodeType.TASK, goal="task B", auto_split=False),
            GraphNode(id="e", type=NodeType.END),
        ],
        edges=[
            GraphEdge(id="e1", from_node="s", to_node="a"),
            GraphEdge(id="e2", from_node="a", to_node="b"),
            GraphEdge(id="e3", from_node="b", to_node="e"),
        ],
    )


def retry_loop_graph():
    """start → task → condition --yes--> end
                        ^---no（回边）---┘
    """
    return make_graph(
        nodes=[
            GraphNode(id="s", type=NodeType.START),
            GraphNode(id="t1", type=NodeType.TASK, goal="scrape", auto_split=False),
            GraphNode(
                id="c1", type=NodeType.CONDITION,
                condition_mode=ConditionMode.JUDGMENT,
                ref_task="t1", criteria="是否返回json文件地址",
            ),
            GraphNode(id="e", type=NodeType.END),
        ],
        edges=[
            GraphEdge(id="e1", from_node="s", to_node="t1"),
            GraphEdge(id="e2", from_node="t1", to_node="c1"),
            GraphEdge(id="e3", from_node="c1", to_node="e", label="yes"),
            GraphEdge(id="e4", from_node="c1", to_node="t1", label="no"),
        ],
    )


def run(executor, graph, **kw):
    return asyncio.run(executor.execute(graph, {}, **kw))


# ---------------------------------------------------------------------------
# 错误分类与格式化
# ---------------------------------------------------------------------------


def test_format_error_preserves_type_for_empty_message():
    # run 9 的黑盒失败：asyncio.TimeoutError 的 str() 是空串
    assert format_error(asyncio.TimeoutError()) == "TimeoutError"
    assert "boom" in format_error(ValueError("boom"))
    assert format_error(ValueError("boom")).startswith("ValueError")


def test_transient_classification():
    assert is_transient_error(asyncio.TimeoutError())
    assert is_transient_error(RuntimeError("Upstream HTTP/2 stream failed"))
    assert is_transient_error(RuntimeError("Network error: Request timed out."))
    assert is_transient_error(ConnectionResetError())
    assert not is_transient_error(ValueError("bad schema"))

    class FakeAuthenticationError(Exception):
        pass

    assert not is_transient_error(FakeAuthenticationError("ssl handshake"))

    class Fake400(Exception):
        status_code = 404

    assert not is_transient_error(Fake400("not found"))


def test_upstream_access_forbidden_is_not_transient_network_jitter():
    from mewcode.client import LLMError

    forbidden = LLMError(
        "API error (502): Error code: 502 - "
        "{'error': {'message': 'Upstream access forbidden, please contact administrator', "
        "'type': 'upstream_error'}, 'type': 'error'}",
        status_code=502,
    )
    assert not is_transient_error(forbidden)

    class FakeUpstreamType(Exception):
        status_code = 502

    assert not is_transient_error(FakeUpstreamType("API error (502): type=upstream_error"))
    assert is_transient_error(LLMError("API error (502): bad gateway", status_code=502))
    assert is_transient_error(RuntimeError("Upstream HTTP/2 stream failed"))


# ---------------------------------------------------------------------------
# 节点重试与错误保真
# ---------------------------------------------------------------------------


def test_transient_error_retried_then_success():
    factory = FakeFactory([
        RuntimeError("Network error: Request timed out."),  # 第 1 次：瞬时失败
        "done: result at C:/out.json",                       # 第 2 次：成功
    ])
    ex = PipelineExecutor(agent_factory=factory)
    rec = run(ex, linear_graph())
    assert rec.status == RunStatus.COMPLETED
    assert len(factory.calls) == 2
    assert "C:/out.json" in rec.node_records["t1"].result_summary


def test_non_transient_error_not_retried():
    factory = FakeFactory([ValueError("bad input"), "never reached"])
    ex = PipelineExecutor(agent_factory=factory)
    rec = run(ex, linear_graph())
    assert rec.status == RunStatus.FAILED
    assert len(factory.calls) == 1  # 没有重试
    assert rec.node_records["t1"].error_message.startswith("ValueError")


def test_empty_str_exception_recorded_with_type_name():
    factory = FakeFactory([asyncio.TimeoutError()])  # str() == ""，且瞬时 → 会重试
    ex = PipelineExecutor(agent_factory=factory)
    rec = run(ex, linear_graph())
    # 瞬时错误重试耗尽 → 网络熔断，标记为可续跑的 INTERRUPTED（非 FAILED）
    assert rec.status == RunStatus.INTERRUPTED
    # 老实现里这里是空字符串，完全无法诊断；现在异常类型名必须可追溯
    assert "TimeoutError" in (rec.error_message or "")
    assert "TimeoutError" in (rec.node_records["t1"].error_message or "")


# ---------------------------------------------------------------------------
# 裁决解析
# ---------------------------------------------------------------------------


def test_parse_verdict():
    p = PipelineExecutor._parse_verdict
    assert p("PASS - 文件存在") is True
    assert p("pass，没问题") is True
    assert p("FAIL - 没有文件") is False
    # 老实现的 bug：回答里出现 PASS 一词就算通过
    assert p("FAIL，因为不满足 PASS 所需条件") is False
    assert p("结论：FAIL。若要 PASS 需要给出文件路径") is False
    assert p("") is False            # fail-closed
    assert p("说不清楚") is False     # fail-closed


def test_judgment_condition_takes_no_branch_on_fail():
    factory = FakeFactory([
        "抓取失败，没有产出文件",                       # t1 第 1 轮
        "FAIL，因为不满足 PASS 所需的 json 文件地址",   # judge 第 1 轮 → no
        "已写入 C:/data.json",                          # t1 第 2 轮（回边重试）
        "PASS - 给出了文件地址",                        # judge 第 2 轮 → yes
    ])
    ex = PipelineExecutor(agent_factory=factory)
    rec = run(ex, retry_loop_graph())
    assert rec.status == RunStatus.COMPLETED
    assert len(factory.calls) == 4  # 走了一次 no 回边


def test_loop_exhaustion_fails_run_instead_of_silent_success():
    def script(idx, prompt):
        if "核验" in prompt:
            return "FAIL - 始终没有文件"
        return "还是没抓到"

    factory = FakeFactory([script])
    ex = PipelineExecutor(agent_factory=factory)
    rec = run(ex, retry_loop_graph())
    # 老实现：回边耗尽 → 队列走空 → COMPLETED（假成功）
    assert rec.status == RunStatus.FAILED
    assert "exhausted" in (rec.error_message or "").lower()


def test_loop_exhaustion_error_type():
    assert issubclass(LoopExhaustedError, Exception)


def test_judgment_call_retried_on_transient_error():
    # 核验（condition/goal）调用也必须受重试保护——
    # 一次网络抖动不应让整条 run 报废
    factory = FakeFactory([
        "已写入 C:/data.json",                          # t1
        RuntimeError("Network error: Request timed out."),  # judge 第 1 次：瞬时失败
        "PASS - 文件地址已给出",                        # judge 重试成功
    ])
    ex = PipelineExecutor(agent_factory=factory)
    rec = run(ex, retry_loop_graph())
    assert rec.status == RunStatus.COMPLETED
    assert len(factory.calls) == 3


# ---------------------------------------------------------------------------
# 断点续跑
# ---------------------------------------------------------------------------


def test_resume_reuses_completed_nodes():
    # 第一次：A 成功，B 瞬时错误耗尽重试 → 网络熔断，run 中断（可续跑）
    f1 = FakeFactory([
        "A result: C:/step_a.json",
        RuntimeError("Upstream HTTP/2 stream failed"),
        RuntimeError("Upstream HTTP/2 stream failed"),
        RuntimeError("Upstream HTTP/2 stream failed"),
    ])
    ex1 = PipelineExecutor(agent_factory=f1)
    rec1 = run(ex1, two_task_graph())
    assert rec1.status == RunStatus.INTERRUPTED
    assert rec1.node_records["a"].status == NodeStatus.COMPLETED
    assert rec1.node_records["b"].status == NodeStatus.FAILED
    assert "a" in rec1.context_outputs  # 完整输出已持久化到记录

    # 续跑：A 直接复用（factory 不再被 A 调用），只有 B 重跑
    f2 = FakeFactory(["B result built on {{a}}"])
    ex2 = PipelineExecutor(agent_factory=f2)
    rec2 = run(ex2, two_task_graph(), resume_from=rec1)
    assert rec2.status == RunStatus.COMPLETED
    assert rec2.resumed_from == rec1.run_id
    assert len(f2.calls) == 1                      # 只有 B 调用了 agent
    assert "task B" in f2.calls[0]
    assert rec2.node_records["a"].status == NodeStatus.COMPLETED
    assert rec2.node_records["b"].status == NodeStatus.COMPLETED
    # A 的输出对下游可用
    assert rec2.context_outputs["a"] == "A result: C:/step_a.json"


def test_resume_seed_used_only_once_for_retry_loops():
    # 续跑后，回边循环第二次到达同一节点时应真正重新执行
    f1 = FakeFactory([
        "scrape ok C:/x.json",
        "PASS ok",
        RuntimeError("Network error: request timed out"),
        RuntimeError("Network error: request timed out"),
        RuntimeError("Network error: request timed out"),
    ])
    g = make_graph(
        nodes=[
            GraphNode(id="s", type=NodeType.START),
            GraphNode(id="t1", type=NodeType.TASK, goal="scrape", auto_split=False),
            GraphNode(
                id="c1", type=NodeType.CONDITION,
                condition_mode=ConditionMode.JUDGMENT,
                ref_task="t1", criteria="ok?",
            ),
            GraphNode(id="t2", type=NodeType.TASK, goal="analyze", auto_split=False),
            GraphNode(id="e", type=NodeType.END),
        ],
        edges=[
            GraphEdge(id="e1", from_node="s", to_node="t1"),
            GraphEdge(id="e2", from_node="t1", to_node="c1"),
            GraphEdge(id="e3", from_node="c1", to_node="t2", label="yes"),
            GraphEdge(id="e4", from_node="c1", to_node="t1", label="no"),
            GraphEdge(id="e5", from_node="t2", to_node="e"),
        ],
    )
    rec1 = run(PipelineExecutor(agent_factory=f1), g)
    # t2 瞬时错误耗尽 → 网络熔断，中断（可续跑）
    assert rec1.status == RunStatus.INTERRUPTED

    # 续跑：t1、c1 复用；t2 重跑成功
    f2 = FakeFactory(["analysis done"])
    rec2 = run(PipelineExecutor(agent_factory=f2), g, resume_from=rec1)
    assert rec2.status == RunStatus.COMPLETED
    assert len(f2.calls) == 1


# ---------------------------------------------------------------------------
# token 计量与预算
# ---------------------------------------------------------------------------


def test_usage_recorded_into_run_record():
    factory = FakeFactory(["ok"], usage={"input_tokens": 111, "output_tokens": 22})
    ex = PipelineExecutor(agent_factory=factory)
    rec = run(ex, linear_graph())
    # 老实现：只发事件不落库，journal 里恒为 0
    assert rec.total_input_tokens == 111
    assert rec.total_output_tokens == 22
    assert rec.node_records["t1"].input_tokens == 111
    assert rec.node_records["t1"].output_tokens == 22


def test_budget_blocks_further_calls():
    factory = FakeFactory(["A ok", "B never"], usage={"input_tokens": 900, "output_tokens": 200})
    ex = PipelineExecutor(agent_factory=factory)
    rec = run(ex, two_task_graph(), budget_total=1000)
    assert rec.status == RunStatus.INTERRUPTED
    assert len(factory.calls) == 1  # A 用掉 1100 > 1000，B 不再发起
    assert "budget" in (rec.error_message or "").lower()
    assert isinstance(BudgetExceededError("x"), Exception)


# ---------------------------------------------------------------------------
# 截断与摘要
# ---------------------------------------------------------------------------


def test_smart_truncate_keeps_tail():
    path = "C:/Users/example-user/final_output.json"
    text = "前情提要 " * 500 + f"最终文件地址：{path}"
    out = PipelineExecutor._smart_truncate(text, 500)
    assert len(out) < len(text)
    assert path in out          # 老实现 text[:500] 会把路径切掉
    assert text[:50] in out     # 开头也保留


def test_result_summary_keeps_tail():
    path = "C:/Users/example-user/final_output.json"
    factory = FakeFactory(["blah " * 400 + f"文件在 {path}"])
    ex = PipelineExecutor(agent_factory=factory)
    rec = run(ex, linear_graph())
    assert path in rec.node_records["t1"].result_summary


# ---------------------------------------------------------------------------
# checkpoint
# ---------------------------------------------------------------------------


def test_checkpoint_called_per_node():
    snapshots = []
    factory = FakeFactory(["ok"])
    ex = PipelineExecutor(
        agent_factory=factory,
        on_checkpoint=lambda rec: snapshots.append(rec.model_dump_json()),
    )
    rec = run(ex, linear_graph())
    assert rec.status == RunStatus.COMPLETED
    # run 开始 1 次 + 每节点完成后 1 次（s/t1/e）+ 结束 1 次 ≥ 5
    assert len(snapshots) >= 5
    # 快照里能看到 t1 的完成状态（崩溃后可恢复）
    assert '"t1"' in snapshots[-1]


def test_checkpoint_failure_does_not_break_run():
    def bad_checkpoint(rec):
        raise OSError("disk full")

    factory = FakeFactory(["ok"])
    ex = PipelineExecutor(agent_factory=factory, on_checkpoint=bad_checkpoint)
    rec = run(ex, linear_graph())
    assert rec.status == RunStatus.COMPLETED  # checkpoint 失败不影响执行


# ---------------------------------------------------------------------------
# output_schema 契约
# ---------------------------------------------------------------------------


def test_output_schema_parsed():
    g = make_graph(
        nodes=[
            GraphNode(id="s", type=NodeType.START),
            GraphNode(
                id="t1", type=NodeType.TASK, goal="scrape", auto_split=False,
                output_schema={"file_path": "string", "record_count": "int"},
            ),
            GraphNode(id="e", type=NodeType.END),
        ],
        edges=[
            GraphEdge(id="e1", from_node="s", to_node="t1"),
            GraphEdge(id="e2", from_node="t1", to_node="e"),
        ],
    )
    factory = FakeFactory(['{"file_path": "C:/d.json", "record_count": 15614}'])
    ex = PipelineExecutor(agent_factory=factory)
    rec = run(ex, g)
    assert rec.status == RunStatus.COMPLETED
    assert rec.context_outputs["t1"]["file_path"] == "C:/d.json"


def test_output_schema_finishing_call_then_fail_if_unparseable():
    g = make_graph(
        nodes=[
            GraphNode(id="s", type=NodeType.START),
            GraphNode(
                id="t1", type=NodeType.TASK, goal="scrape", auto_split=False,
                output_schema={"file_path": "string"},
            ),
            GraphNode(id="e", type=NodeType.END),
        ],
        edges=[
            GraphEdge(id="e1", from_node="s", to_node="t1"),
            GraphEdge(id="e2", from_node="t1", to_node="e"),
        ],
    )
    # 主调用输出叙事文本 → 触发格式化收尾 → 仍不是 JSON → 节点失败
    factory = FakeFactory(["我完成了抓取……", "还是叙事，没有 JSON"])
    ex = PipelineExecutor(agent_factory=factory)
    rec = run(ex, g)
    assert rec.status == RunStatus.FAILED
    assert "output_schema" in (rec.error_message or "")

    # 收尾调用给出 JSON → 成功
    factory2 = FakeFactory(["我完成了抓取……", '{"file_path": "C:/d.json"}'])
    ex2 = PipelineExecutor(agent_factory=factory2)
    rec2 = run(ex2, g)
    assert rec2.status == RunStatus.COMPLETED
    assert rec2.context_outputs["t1"]["file_path"] == "C:/d.json"


# ---------------------------------------------------------------------------
# 节点超时
# ---------------------------------------------------------------------------


def test_node_timeout():
    class SlowFactory(FakeFactory):
        async def __call__(self, prompt, **kw):
            self.calls.append(prompt)
            await asyncio.sleep(5)
            return "too late", dict(self.usage)

    g = linear_graph()
    g.nodes[1].timeout_seconds = 1  # t1 限时 1 秒
    ex = PipelineExecutor(agent_factory=SlowFactory(["x"]))
    rec = run(ex, g)
    assert rec.status == RunStatus.FAILED
    assert "time limit" in (rec.error_message or "").lower()


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
