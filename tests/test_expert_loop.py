"""端到端：专家团 v3 全链路集成。

没有真实 LLM 参与，但把 v3 的每一块机制串成一条完整叙事并断言可观测产物：
结构化审计 → 有界辩论 → 管理终裁 → 定向返工 → PASS；上报链；自建 tool/skill
权限硬 gate；编排 DSL；检查点 flow_state 恢复续跑。
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

from mewcode.expert_dsl import Runtime
from mewcode.expert_group import (
    ExpertGroupOrchestrator,
    ExpertProfile,
    ExpertRoleResult,
)
from mewcode.experts import (
    DECISION_APPROVE,
    DECISION_DEFER,
    OUTCOME_GRANTED,
    ArtifactNotExecutable,
    EscalationChain,
    ExpertArtifactWorkspace,
)
from mewcode.model_registry import ModelRef


def profile() -> ExpertProfile:
    return ExpertProfile(
        manager=ModelRef("openai", "gpt-5.6", "high"),
        executor=ModelRef("openai", "gpt-5.6", "medium"),
        auditor=ModelRef("anthropic", "claude-sonnet-5", ""),
    )


CATALOG: dict = {"providers": []}


def verdict_pass() -> dict:
    return {"verdict": "PASS", "issues": [], "evidence": []}


def verdict_needs() -> dict:
    return {
        "verdict": "NEEDS_ATTENTION",
        "issues": [{"file": "mewcode/x.py", "description": "tests fail"}],
        "evidence": [{"kind": "test", "command": "pytest", "exit_code": 1}],
    }


@pytest.fixture
def scratch():
    base = Path(".artifacts_scratch") / "expert-loop" / uuid.uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    yield base
    shutil.rmtree(base, ignore_errors=True)


@pytest.mark.asyncio
async def test_full_loop_repairs_and_passes(scratch) -> None:
    """审计失败 → 两轮辩论 → 终裁 REWORK → 一轮定向返工 → PASS。"""
    calls = []
    events = []
    auditor_runs = {"count": 0}

    async def runner(role, model_ref, prompt, policy):
        calls.append(role)
        if role == "manager" and "由你终裁" in prompt:
            return ExpertRoleResult(text="FINAL_RULING: REWORK")
        if role == "auditor":
            auditor_runs["count"] += 1
            verdict = (
                verdict_pass() if auditor_runs["count"] >= 4 else verdict_needs()
            )
            return ExpertRoleResult(text="audit", verdict=verdict)
        return ExpertRoleResult(text=f"{role} result")

    orchestrator = ExpertGroupOrchestrator(
        runner=runner,
        event_sink=lambda event: events.append(event),
        archive_root=scratch / "runs",
    )
    result = await orchestrator.run(
        "fix the failing module", profile(), CATALOG, strategy="stable"
    )

    # 四段 + 两轮辩论 + 终裁 + 一轮返工（executor + 复审）
    assert calls == [
        "manager", "planner", "executor", "auditor",
        "planner", "auditor",
        "planner", "auditor",
        "manager",
        "executor", "auditor",
    ]
    assert result.status == "completed"

    # 归档可观测：各阶段 fallback_reason 齐全
    archive = json.loads(
        (scratch / "runs" / f"{result.run_id}.json").read_text(encoding="utf-8")
    )
    reasons = [stage.get("fallback_reason") for stage in archive["stages"]]
    assert "debate" in reasons
    assert "final_ruling" in reasons
    assert "rework" in reasons

    # flow_state 事件收尾正确
    flow_events = [
        event for event in events if event.get("type") == "expert_flow_state"
    ]
    assert flow_events[-1]["phase"] == "done"
    assert flow_events[-1]["passed"] is True


def test_escalation_chain_grants_after_planner_approval() -> None:
    chain = EscalationChain()
    chain.decide("subagent", DECISION_DEFER, by="sub-a")
    chain.decide("executor", DECISION_DEFER, by="exec-b")
    chain.decide("planner", DECISION_APPROVE, reason="低风险", by="plan-c")
    assert chain.outcome == OUTCOME_GRANTED
    assert len(chain.trace) == 3


def test_self_built_skill_hard_gate(scratch) -> None:
    workspace = ExpertArtifactWorkspace.for_run(scratch, "run-1")
    artifact = workspace.write_skill(
        "helper",
        "---\nname: helper\ndescription: A helper for e2e.\n---\n\nBody.\n",
    )
    artifact.validate()
    with pytest.raises(ArtifactNotExecutable):
        artifact.require_executable()
    artifact.approve("executor")
    artifact.require_executable()  # 批准后放行


@pytest.mark.asyncio
async def test_dsl_program_runs_on_host() -> None:
    calls = []

    class Host:
        async def spawn(self, role, task):
            calls.append(("spawn", role, task))
            return "h"

        async def fanout(self, role, tasks):
            calls.append(("fanout", role, tasks))
            return ["h"] * len(tasks)

        async def wait(self):
            calls.append(("wait",))
            return ["done"]

        async def escalate(self, level, issue):
            calls.append(("escalate", level, issue))
            return "ok"

        async def verify(self, scope):
            calls.append(("verify", scope))
            return {"verdict": "PASS"}

        async def evaluate(self, key):
            calls.append(("evaluate", key))
            return key == "go"

    program = [
        {"op": "fanout", "role": "executor", "tasks": ["a", "b"]},
        {"op": "wait"},
        {"op": "branch", "on": "go", "then": [{"op": "verify", "scope": "final"}]},
    ]
    runtime = Runtime(Host())
    await runtime.run(program)
    assert [call[0] for call in calls] == ["fanout", "wait", "evaluate", "verify"]
    assert runtime.steps_run == 4


@pytest.mark.asyncio
async def test_resume_continues_flow_from_checkpoint(scratch) -> None:
    """暂停在返工第 1 轮后恢复：续跑第 2 轮并修复。"""
    calls = []

    async def runner(role, model_ref, prompt, policy):
        calls.append(role)
        return ExpertRoleResult(
            text=f"{role} result",
            verdict=verdict_pass() if role == "auditor" else None,
        )

    checkpoint = {
        "stages": {
            "manager": {"status": "completed", "text": "m"},
            "planner": {"status": "completed", "text": "p"},
            "executor": {"status": "completed", "text": "e"},
            "auditor": {
                "status": "needs_attention",
                "text": "issues",
                "verdict": verdict_needs(),
            },
        },
        "flow_state": {
            "phase": "rework",
            "debate_rounds": 2,
            "rework_rounds": 1,
            "ruling_text": "FINAL_RULING: REWORK",
            "ruling_done": True,
            "last_verdict": verdict_needs(),
            "passed": False,
        },
    }
    orchestrator = ExpertGroupOrchestrator(
        runner=runner, archive_root=scratch / "runs"
    )
    result = await orchestrator.run(
        "resume", profile(), CATALOG, strategy="stable",
        checkpoint=checkpoint, run_id="a" * 32, attempt=2,
    )
    assert calls == ["executor", "auditor"]
    assert result.status == "completed"
