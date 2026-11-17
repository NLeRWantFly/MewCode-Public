from __future__ import annotations

import asyncio
import shutil
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from mewcode.expert_group import (
    EXPERT_ROLE_CONTRACTS,
    MAX_DEBATE_ROUNDS,
    REPORT_STYLE_CONTRACT,
    ExpertGroupOrchestrator,
    ExpertProfile,
    ExpertProfileStore,
    ExpertRoleResult,
    ExpertRunBudget,
    classify_task,
    derive_execution_policy,
    executor_max_iterations,
    recovery_executor_max_iterations,
    recovery_executor_timeout_seconds,
    select_planner_model,
)
from mewcode.model_registry import ModelRef


@pytest.fixture
def scratch():
    """Per-test writable dir (sandbox-safe permissive-mode creation)."""
    base = Path(".artifacts_scratch") / "expert-group" / uuid.uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    yield base
    shutil.rmtree(base, ignore_errors=True)


def profile() -> ExpertProfile:
    return ExpertProfile(
        manager=ModelRef("openai", "gpt-5.6", "high"),
        executor=ModelRef("openai", "gpt-5.6", "medium"),
        auditor=ModelRef("anthropic", "claude-sonnet-5", ""),
    )


CATALOG = {
    "providers": [
        {
            "key": "moonshot",
            "models": [{"id": "kimi-k3", "capabilities": ["chat"]}],
        },
        {
            "key": "anthropic",
            "models": [{"id": "claude-opus-5", "capabilities": ["chat"]}],
        },
    ]
}


def verdict_pass() -> dict:
    return {"verdict": "PASS", "issues": [], "evidence": []}


def verdict_needs() -> dict:
    return {
        "verdict": "NEEDS_ATTENTION",
        "issues": [{"file": "mewcode/x.py", "description": "tests fail"}],
        "evidence": [{"kind": "test", "command": "pytest", "exit_code": 1}],
    }


def test_debate_cap_is_two() -> None:
    assert MAX_DEBATE_ROUNDS == 2


def test_report_style_contract_nudges_report_step_and_ask_user() -> None:
    assert "report_step" in REPORT_STYLE_CONTRACT
    assert "AskUserQuestion" in REPORT_STYLE_CONTRACT
    assert "human_oncall" in REPORT_STYLE_CONTRACT


def test_task_classification_and_hard_coded_routing():
    assert classify_task("build a React UI component") == "frontend"
    assert classify_task("secure the backend API") == "backend"
    frontend, _ = select_planner_model(profile().manager, CATALOG, "frontend")
    backend, _ = select_planner_model(profile().manager, CATALOG, "backend")
    assert frontend.model_id == "kimi-k3"
    assert backend.model_id == "claude-opus-5"


def test_planner_falls_back_without_inventing_a_model():
    selected, reason = select_planner_model(profile().manager, {"providers": []}, "other")
    assert selected == profile().manager
    assert "fell back" in reason


def test_user_planner_route_precedes_built_in_brand_preferences():
    preferred = ModelRef("anthropic", "claude-opus-5", "")
    selected, reason = select_planner_model(
        profile().manager, CATALOG, "frontend", [preferred]
    )
    assert selected == preferred
    assert "user planner route" in reason


@pytest.mark.asyncio
async def test_explicit_planner_can_use_any_registered_brand(scratch):
    calls = []
    selected = ExpertProfile(
        manager=profile().manager,
        planner=ModelRef("deepseek", "deepseek-reasoner", ""),
        executor=profile().executor,
        auditor=profile().auditor,
    )

    async def runner(role, model_ref, prompt, policy):
        calls.append((role, model_ref))
        return ExpertRoleResult(
            text="ok", verdict=verdict_pass() if role == "auditor" else None
        )

    orchestrator = ExpertGroupOrchestrator(
        runner=runner,
        archive_root=scratch / "runs",
    )
    await orchestrator.run("general task", selected, CATALOG, strategy="stable")
    assert calls[1] == ("planner", selected.planner)


def test_profile_store_is_keyed_by_normalized_project(scratch):
    store = ExpertProfileStore(scratch / "profiles.json")
    store.put(scratch / "project", profile())
    assert store.get(scratch / "project") == profile()


def test_profile_store_round_trips_optional_capability_executor(scratch):
    store = ExpertProfileStore(scratch / "profiles.json")
    selected = replace(
        profile(),
        capability_executor=ModelRef("xai", "grok-4.6", "high"),
    )
    store.put(scratch / "project", selected)
    assert store.get(scratch / "project") == selected
    assert store.get(scratch / "project").as_dict()["capability_executor"] == {
        "provider_key": "xai",
        "model_id": "grok-4.6",
        "effort": "high",
    }


@pytest.mark.asyncio
async def test_fixed_order_stable_policy_and_single_audit(scratch):
    calls = []
    events = []

    async def runner(role, model_ref, prompt, policy):
        calls.append((role, policy))
        return ExpertRoleResult(
            text=f"{role} result",
            input_tokens=1,
            output_tokens=2,
            verdict=verdict_pass() if role == "auditor" else None,
        )

    orchestrator = ExpertGroupOrchestrator(
        runner=runner,
        event_sink=lambda event: events.append(event),
        archive_root=scratch / "runs",
    )
    result = await orchestrator.run(
        "build a frontend UI", profile(), CATALOG, strategy="stable"
    )

    assert [role for role, _ in calls] == ["manager", "planner", "executor", "auditor"]
    assert calls[2][1].allow_delegation is False
    assert calls[2][1].max_direct_agents == 0
    assert len([role for role, _ in calls if role == "auditor"]) == 1
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_needs_attention_triggers_bounded_debate_and_final_ruling(scratch):
    calls = []

    async def runner(role, model_ref, prompt, policy):
        calls.append((role, policy))
        return ExpertRoleResult(
            text=f"{role} result",
            verdict=verdict_needs() if role == "auditor" else None,
        )

    orchestrator = ExpertGroupOrchestrator(
        runner=runner, archive_root=scratch / "runs"
    )
    result = await orchestrator.run(
        "refactor code", profile(), CATALOG, strategy="fast"
    )

    # 初始四段 + 两轮辩论 + 管理终裁（无标记→返工）+ 三轮定向返工
    assert [role for role, _ in calls] == [
        "manager", "planner", "executor", "auditor",
        "planner", "auditor",
        "planner", "auditor",
        "manager",
        "executor", "auditor",
        "executor", "auditor",
        "executor", "auditor",
    ]
    assert calls[2][1].allow_delegation is True
    assert calls[2][1].max_direct_agents == 8
    assert result.status == "needs_attention"
    assert "[管理终裁]" in result.audit_summary
    assert "定向返工已达上限，问题仍未解决，请人工介入。" in result.audit_summary


@pytest.mark.asyncio
async def test_accept_ruling_ends_without_rework(scratch):
    calls = []

    async def runner(role, model_ref, prompt, policy):
        calls.append(role)
        if role == "manager" and "由你终裁" in prompt:
            return ExpertRoleResult(text="FINAL_RULING: ACCEPT")
        return ExpertRoleResult(
            text=f"{role} result",
            verdict=verdict_needs() if role == "auditor" else None,
        )

    orchestrator = ExpertGroupOrchestrator(
        runner=runner, archive_root=scratch / "runs"
    )
    result = await orchestrator.run("x", profile(), CATALOG, strategy="stable")

    assert calls == [
        "manager", "planner", "executor", "auditor",
        "planner", "auditor",
        "planner", "auditor",
        "manager",
    ]
    assert result.status == "completed"
    assert "定向返工" not in result.audit_summary


@pytest.mark.asyncio
async def test_rework_repairs_and_passes_after_one_round(scratch):
    calls = []
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
            return ExpertRoleResult(text="ok", verdict=verdict)
        return ExpertRoleResult(text=f"{role} result")

    orchestrator = ExpertGroupOrchestrator(
        runner=runner, archive_root=scratch / "runs"
    )
    result = await orchestrator.run("x", profile(), CATALOG, strategy="stable")

    # 四段 + 两轮辩论 + 终裁 REWORK + 一轮定向返工后复审 PASS
    assert calls == [
        "manager", "planner", "executor", "auditor",
        "planner", "auditor",
        "planner", "auditor",
        "manager",
        "executor", "auditor",
    ]
    assert result.status == "completed"
    assert "定向返工已达上限" not in result.audit_summary


@pytest.mark.asyncio
async def test_debate_can_resolve_to_pass_without_ruling(scratch):
    calls = []
    auditor_runs = {"count": 0}

    async def runner(role, model_ref, prompt, policy):
        calls.append(role)
        verdict = None
        if role == "auditor":
            auditor_runs["count"] += 1
            verdict = (
                verdict_pass() if auditor_runs["count"] > 1 else verdict_needs()
            )
        return ExpertRoleResult(text=f"{role} result", verdict=verdict)

    orchestrator = ExpertGroupOrchestrator(
        runner=runner, archive_root=scratch / "runs"
    )
    result = await orchestrator.run(
        "refactor", profile(), CATALOG, strategy="stable"
    )

    # 一轮辩论后复审 PASS，不再终裁
    assert calls == ["manager", "planner", "executor", "auditor", "planner", "auditor"]
    assert result.status == "completed"
    assert "[管理终裁]" not in result.audit_summary


@pytest.mark.asyncio
async def test_fresh_audit_without_structured_verdict_is_an_anomaly(scratch):
    """未提交结构化结论的新审计不得误判 PASS。"""
    calls = []

    async def runner(role, model_ref, prompt, policy):
        calls.append(role)
        return ExpertRoleResult(text=f"{role} result")  # 无结构化结论

    orchestrator = ExpertGroupOrchestrator(
        runner=runner, archive_root=scratch / "runs"
    )
    result = await orchestrator.run("x", profile(), CATALOG, strategy="stable")
    assert result.status == "needs_attention"


@pytest.mark.asyncio
async def test_legacy_restored_completed_audit_still_passes(scratch):
    """旧检查点（无结构化结论）恢复时按持久化状态信任，不误伤。"""
    calls = []

    async def runner(role, model_ref, prompt, policy):
        calls.append(role)
        return ExpertRoleResult(text=f"{role} result")

    checkpoint = {
        "stages": {
            "manager": {"status": "completed", "text": "m"},
            "planner": {"status": "completed", "text": "p"},
            "auditor": {"status": "completed", "text": "old audit"},
        }
    }
    orchestrator = ExpertGroupOrchestrator(
        runner=runner, archive_root=scratch / "runs"
    )
    result = await orchestrator.run(
        "resume", profile(), CATALOG, strategy="stable",
        checkpoint=checkpoint, run_id="a" * 32, attempt=2,
    )
    assert calls == ["executor"]
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_cancellation_propagates_once_and_emits_cancelled(scratch):
    entered = asyncio.Event()
    release = asyncio.Event()
    cancelled = []
    events = []

    async def runner(role, model_ref, prompt, policy):
        entered.set()
        await release.wait()
        return ExpertRoleResult(text="never reached")

    async def cancel_direct_agents():
        cancelled.append(True)

    orchestrator = ExpertGroupOrchestrator(
        runner=runner,
        event_sink=lambda event: events.append(event),
        archive_root=scratch / "runs",
        cancel_callback=cancel_direct_agents,
    )
    task = asyncio.create_task(
        orchestrator.run("cancel this run", profile(), CATALOG, strategy="stable")
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled == [True]
    assert any(
        event.get("type") == "expert_run_done"
        and event.get("status") == "paused"
        for event in events
    )


def test_multi_agent_switch_maps_one_to_one_without_strategy_prompt():
    stable = derive_execution_policy(False)
    fast = derive_execution_policy(True)
    assert (stable.strategy, stable.allow_delegation, stable.max_direct_agents) == (
        "stable", False, 0,
    )
    assert (fast.strategy, fast.allow_delegation, fast.max_direct_agents) == (
        "fast", True, 8,
    )
    assert fast.allow_recursive_delegation is False


@pytest.mark.asyncio
async def test_finalizer_skip_marks_run_blocked_with_predecessor_details(scratch):
    """Unsatisfied finalizer predecessors leave a resumable blocked run."""
    from mewcode.experts.flow import ExpertFlowSpec

    async def node_runner(node, _model, _prompt, _policy, member=None):
        if node.node_id == "stage_a":
            return ExpertRoleResult(text="A ok", status="completed")
        if node.node_id == "stage_b":
            return ExpertRoleResult(text="B blocked", status="needs_attention")
        return ExpertRoleResult(text=f"ok:{node.node_id}")

    async def fallback_runner(*_args):
        raise AssertionError("node_runner should own graph execution")

    flow = ExpertFlowSpec.from_value({
        "flow_id": "test.finalizer.blocked",
        "version": 1,
        "label": "Blocked finalizer",
        "nodes": [
            {"node_id": "stage_a", "role": "executor", "label": "阶段A"},
            {"node_id": "stage_b", "role": "executor", "label": "阶段B"},
            {"node_id": "final", "role": "manager", "label": "汇总"},
        ],
        "edges": [
            {"from": "stage_a", "to": "final"},
            {"from": "stage_b", "to": "final"},
        ],
        "finalizer_node_id": "final",
    })
    result = await ExpertGroupOrchestrator(
        runner=fallback_runner,
        node_runner=node_runner,
        archive_root=scratch / "blocked-finalizer",
    ).run("任务", profile(), CATALOG, strategy="stable", flow=flow)
    assert result.status == "blocked"
    assert "B blocked" in result.final_summary
    assert "阶段B" in result.final_summary
    assert "继续任务" in result.final_summary
    assert "Finalizer skipped" not in result.final_summary


@pytest.mark.asyncio
async def test_mid_chain_failure_does_not_jump_to_finalizer(scratch):
    """A failed mid-stage must stop there instead of emitting a later-stage skip."""
    from mewcode.experts.flow import ExpertFlowSpec

    calls: list[str] = []
    phases: list[str] = []

    async def node_runner(node, _model, _prompt, _policy, member=None):
        calls.append(node.node_id)
        if node.node_id == "confirm_intro":
            return ExpertRoleResult(
                text="API error (400): unknown provider for model gemini-3.8-flash",
                status="failed",
            )
        return ExpertRoleResult(text=f"ok:{node.node_id}", status="completed")

    async def fallback_runner(*_args):
        raise AssertionError("node_runner should own graph execution")

    def sink(event):
        if event.get("type") == "expert_flow_state" and event.get("phase"):
            phases.append(str(event["phase"]))

    flow = ExpertFlowSpec.from_value({
        "flow_id": "test.halt.midchain",
        "version": 1,
        "label": "Mid-chain halt",
        "nodes": [
            {"node_id": "done", "role": "executor", "label": "已完成"},
            {"node_id": "confirm_intro", "role": "executor", "label": "确认摘要引言"},
            {"node_id": "finalize_dual_format", "role": "executor", "label": "双格式定稿"},
            {"node_id": "audit_final_pack", "role": "manager", "label": "验收定稿包"},
        ],
        "edges": [
            {"from": "done", "to": "confirm_intro"},
            {"from": "confirm_intro", "to": "finalize_dual_format"},
            {"from": "finalize_dual_format", "to": "audit_final_pack"},
        ],
        "finalizer_node_id": "audit_final_pack",
    })
    result = await ExpertGroupOrchestrator(
        runner=fallback_runner,
        node_runner=node_runner,
        event_sink=sink,
        archive_root=scratch / "halt-midchain",
    ).run(
        "任务",
        profile(),
        CATALOG,
        strategy="stable",
        flow=flow,
        checkpoint={
            "node_states": {
                "done": {"status": "completed", "text": "already done"},
            }
        },
    )
    assert result.status == "blocked"
    assert "confirm_intro" in calls
    assert "finalize_dual_format" not in calls
    assert "audit_final_pack" not in calls
    assert "finalizer_skipped" not in phases
    assert "unknown provider" in result.final_summary
    assert "确认摘要引言" in result.final_summary
    assert "双格式定稿" not in result.final_summary
    assert "Finalizer skipped" not in result.final_summary


@pytest.mark.asyncio
async def test_resume_keeps_node_effort_when_locked_model_omits_it(scratch):
    """Resume must not drop Gemini effort just because the checkpoint lock omitted it."""
    from mewcode.experts.flow import ExpertFlowSpec

    seen: list[ModelRef] = []
    started: list[dict] = []

    async def node_runner(node, model_ref, _prompt, _policy, member=None):
        if node.node_id == "confirm":
            seen.append(model_ref)
        return ExpertRoleResult(text="ok", status="completed")

    async def fallback_runner(*_args):
        raise AssertionError("node_runner should own graph execution")

    def sink(event):
        if (
            event.get("type") in {"expert_node_started", "expert_stage"}
            and event.get("node_id") == "confirm"
            and event.get("status") == "running"
        ):
            started.append(event)

    flow = ExpertFlowSpec.from_value({
        "flow_id": "test.locked.effort",
        "version": 1,
        "label": "Locked effort",
        "nodes": [
            {
                "node_id": "confirm",
                "role": "executor",
                "label": "确认摘要引言",
                "model": {
                    "provider_key": "desktop:gemini",
                    "model_id": "gemini-3.8-flash",
                    "effort": "high",
                },
            },
            {"node_id": "final", "role": "manager", "label": "汇总"},
        ],
        "edges": [{"from": "confirm", "to": "final"}],
        "finalizer_node_id": "final",
    })
    result = await ExpertGroupOrchestrator(
        runner=fallback_runner,
        node_runner=node_runner,
        event_sink=sink,
        archive_root=scratch / "locked-effort",
    ).run(
        "任务",
        profile(),
        CATALOG,
        strategy="stable",
        flow=flow,
        checkpoint={
            "locked_models": {
                "confirm": {
                    "provider_key": "desktop:gemini",
                    "model_id": "gemini-3.8-flash",
                    "effort": "",
                }
            }
        },
    )
    assert result.status == "completed"
    assert seen and seen[0].effort == "high"
    assert seen[0].model_id == "gemini-3.8-flash"
    assert started
    assert all(event.get("effort") == "high" for event in started)


@pytest.mark.asyncio
async def test_resume_skips_completed_but_reruns_needs_attention(scratch):
    """Checkpoint resume must not treat HITL stages as permanently done."""
    from mewcode.experts.flow import ExpertFlowSpec

    calls: list[str] = []

    async def node_runner(node, _model, _prompt, _policy, member=None):
        calls.append(node.node_id)
        return ExpertRoleResult(text=f"ran:{node.node_id}", status="completed")

    async def fallback_runner(*_args):
        raise AssertionError("node_runner should own graph execution")

    flow = ExpertFlowSpec.from_value({
        "flow_id": "test.resume.skip",
        "version": 1,
        "label": "Resume skip",
        "nodes": [
            {"node_id": "done", "role": "executor", "label": "已完成"},
            {"node_id": "hitl", "role": "executor", "label": "待处理"},
            {"node_id": "final", "role": "manager", "label": "汇总"},
        ],
        "edges": [
            {"from": "done", "to": "hitl"},
            {"from": "hitl", "to": "final"},
        ],
        "finalizer_node_id": "final",
    })
    checkpoint = {
        "node_states": {
            "done": {"status": "completed", "text": "already done"},
            "hitl": {"status": "needs_attention", "text": "waiting human"},
        }
    }
    result = await ExpertGroupOrchestrator(
        runner=fallback_runner,
        node_runner=node_runner,
        archive_root=scratch / "resume-skip",
    ).run(
        "任务",
        profile(),
        CATALOG,
        strategy="stable",
        flow=flow,
        checkpoint=checkpoint,
    )
    assert result.status == "completed"
    assert "done" not in calls  # restored from checkpoint
    assert "hitl" in calls  # must re-run after HITL
    assert "final" in calls


@pytest.mark.asyncio
async def test_constraint_pipeline_parent_allows_limited_delegation_in_stable(scratch):
    """Folded refine children unlock limited Agent spawn on the parent executor."""
    from mewcode.experts.flow import ExpertFlowSpec

    calls: list[tuple[str, object, str]] = []

    async def runner(role, model_ref, prompt, policy):
        calls.append((role, policy, prompt))
        return ExpertRoleResult(text=f"{role} done")

    flow = ExpertFlowSpec.from_value({
        "flow_id": "test.constraint.parent",
        "version": 1,
        "label": "Constraint parent",
        "nodes": [
            {
                "node_id": "parent",
                "role": "executor",
                "label": "父阶段",
                "responsibility": "完成父任务\n约束管线（子阶段）：\n1. [子A] 做A",
            },
            {
                "node_id": "final",
                "role": "manager",
                "label": "收束",
                "responsibility": "汇总",
            },
        ],
        "edges": [{"from": "parent", "to": "final"}],
        "finalizer_node_id": "final",
        "meta": {
            "schedule_skipped_node_ids": ["child_a", "child_b"],
            "constraint_pipelines": {
                "parent": [
                    {"node_id": "child_a", "label": "子A", "goal": "做A"},
                    {"node_id": "child_b", "label": "子B", "goal": "做B"},
                ]
            },
        },
    })
    result = await ExpertGroupOrchestrator(
        runner=runner, archive_root=scratch / "runs"
    ).run("父任务", profile(), CATALOG, strategy="stable", flow=flow)
    assert result.status == "completed"
    parent_calls = [
        (policy, prompt) for role, policy, prompt in calls if role == "executor"
    ]
    assert parent_calls
    policy, prompt = parent_calls[0]
    assert policy.allow_delegation is True
    assert policy.max_direct_agents == 2
    assert "约束管线" in prompt
    assert "按需创建直属子 agent" in prompt


def test_work_roles_can_use_tools_and_audit_remains_independent():
    assert EXPERT_ROLE_CONTRACTS["manager"].tools == ("*",)
    assert EXPERT_ROLE_CONTRACTS["planner"].tools == ("*",)
    assert "Bash" not in EXPERT_ROLE_CONTRACTS["auditor"].tools
    assert "InspectGitState" in EXPERT_ROLE_CONTRACTS["auditor"].tools
    assert EXPERT_ROLE_CONTRACTS["executor"].may_write is True


@pytest.mark.asyncio
async def test_resume_continues_rework_from_saved_round(scratch):
    """恢复时从检查点的返工轮次继续，而非从头重跑辩论与返工。"""
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

    # 只续跑返工第 2 轮（executor + 复审），辩论与终裁不重来
    assert calls == ["executor", "auditor"]
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_deadline_budget_stops_loops_and_escalates(scratch):
    """整体预算耗尽：跳过辩论/终裁/返工，直接升级给人。"""
    calls = []

    async def runner(role, model_ref, prompt, policy):
        calls.append(role)
        return ExpertRoleResult(
            text=f"{role} result",
            verdict=verdict_needs() if role == "auditor" else None,
        )

    orchestrator = ExpertGroupOrchestrator(
        runner=runner, archive_root=scratch / "runs"
    )
    result = await orchestrator.run(
        "x", profile(), CATALOG, strategy="stable",
        budget=ExpertRunBudget(deadline_seconds=0),
    )

    assert calls == ["manager", "planner", "executor", "auditor"]
    assert result.status == "needs_attention"
    assert "运行预算耗尽，请人工介入。" in result.audit_summary


@pytest.mark.asyncio
async def test_flow_state_events_are_emitted(scratch):
    events = []

    async def runner(role, model_ref, prompt, policy):
        return ExpertRoleResult(
            text=f"{role} result",
            verdict=verdict_needs() if role == "auditor" else None,
        )

    orchestrator = ExpertGroupOrchestrator(
        runner=runner,
        event_sink=lambda event: events.append(event),
        archive_root=scratch / "runs",
    )
    await orchestrator.run("x", profile(), CATALOG, strategy="stable")

    flow_events = [
        event for event in events if event.get("type") == "expert_flow_state"
    ]
    assert flow_events, "应发出 flow_state 事件"
    last = flow_events[-1]
    assert last["phase"] == "done"
    assert last["rework_rounds"] == 3
    assert last["passed"] is False
    assert last["ruling_done"] is True


def test_executor_iteration_budget_is_independent_and_configurable(monkeypatch):
    monkeypatch.delenv("MEWCODE_EXPERT_EXECUTOR_MAX_ITERATIONS", raising=False)
    assert executor_max_iterations(50) == 200
    assert executor_max_iterations(300) == 300
    monkeypatch.setenv("MEWCODE_EXPERT_EXECUTOR_MAX_ITERATIONS", "123")
    assert executor_max_iterations(50) == 123
    monkeypatch.setenv("MEWCODE_EXPERT_EXECUTOR_MAX_ITERATIONS", "10")
    assert executor_max_iterations(50) == 50
    monkeypatch.setenv("MEWCODE_EXPERT_EXECUTOR_MAX_ITERATIONS", "abc")
    assert executor_max_iterations(50) == 200


def test_recovery_executor_iterations_are_strictly_bounded(monkeypatch):
    monkeypatch.delenv("MEWCODE_EXPERT_RECOVERY_MAX_ITERATIONS", raising=False)
    assert recovery_executor_max_iterations() == 40
    monkeypatch.setenv("MEWCODE_EXPERT_RECOVERY_MAX_ITERATIONS", "24")
    assert recovery_executor_max_iterations() == 24
    monkeypatch.setenv("MEWCODE_EXPERT_RECOVERY_MAX_ITERATIONS", "999")
    assert recovery_executor_max_iterations() == 100
    monkeypatch.delenv("MEWCODE_EXPERT_RECOVERY_TIMEOUT_SECONDS", raising=False)
    assert recovery_executor_timeout_seconds() == 3600.0
    monkeypatch.setenv("MEWCODE_EXPERT_RECOVERY_TIMEOUT_SECONDS", "300")
    assert recovery_executor_timeout_seconds() == 300.0


@pytest.mark.asyncio
async def test_budget_exhausted_executor_forces_needs_attention(scratch):
    prompts = {}

    async def runner(role, model_ref, prompt, policy):
        prompts[role] = prompt
        if role == "executor":
            return ExpertRoleResult(text="partial work", status="budget_exhausted")
        if role == "auditor":
            return ExpertRoleResult(text="audit ok", verdict=verdict_pass())
        return ExpertRoleResult(text=f"{role} ok")

    orchestrator = ExpertGroupOrchestrator(
        runner=runner, archive_root=scratch / "runs"
    )
    result = await orchestrator.run(
        "general task", profile(), CATALOG, strategy="stable"
    )
    assert result.status == "needs_attention"
    assert "轮数上限" in prompts["auditor"]
