"""Recover a model-call slice limit without abandoning the running workflow.

This does not grant tools, relax acceptance, or increase a run/loop budget.
The shared controller chooses continuation, assistance, or a host-owned human gate.
"""
from __future__ import annotations

import asyncio
import json
import inspect
import uuid
from dataclasses import replace

from mewcode.agent import AskUserRequest, ErrorEvent, TurnComplete


CONTINUE = "继续当前任务（不扩大权限）"
PAUSE = "暂停并保留进度"
RECOVERY_TOOL = "IterationRecovery"


def accept_recovery_answer(state, answers):
    if not isinstance(answers, dict) or answers.get("action") != CONTINUE:
        state["phase"] = "paused"
        return False
    state.update(phase="running", human_continuations=int(state.get("human_continuations", 0)) + 1)
    return True


async def recover_iteration_stream(agent, conversation, *, state, save, emit, ask,
                                   remaining, progress, node_id, role, batch_handoff=False,
                                   before_resume=None, decide=None, adapt=None,
                                   finish_probe=None, probe_only=False, requested=None, controller=None):
    """Continue productive segments; return escalations to the owning scheduler."""
    from dataclasses import asdict
    from mewcode.kernel.escalation import decide_and_record
    original_limit = getattr(agent, "max_iterations", 0)
    turn_offset = 0
    async def checkpoint():
        pending = save()
        if inspect.isawaitable(pending):
            await pending
    try:
        while True:
            if before_resume is not None:
                await before_resume()
            if turn_offset and not remaining():
                yield ErrorEvent("剩余资源不足，检查点已保存", kind="max_iterations")
                return
            stream = agent.run(conversation)
            exhausted, explicit_request, segment_turn = None, False, 0
            try:
                async for event in stream:
                    if isinstance(event, ErrorEvent) and event.kind in {"max_iterations", "soft_deadline"}:
                        exhausted = event
                        break
                    if isinstance(event, TurnComplete):
                        segment_turn = event.turn
                        event = replace(event, turn=turn_offset + event.turn)
                    yield event
                    if isinstance(event, TurnComplete) and requested is not None:
                        pending = requested()
                        if inspect.isawaitable(pending):
                            pending = await pending
                        if pending:
                            explicit_request = True
                            exhausted = ErrorEvent("请求主模型协助调整执行方法", kind="max_iterations")
                            break
            finally:
                await stream.aclose()
            if exhausted is None:
                return
            turn_offset += max(0, segment_turn or original_limit or 0)
            state["slice_exhaustions"] = int(state.get("slice_exhaustions", 0)) + 1
            current = progress() or {}
            await checkpoint()
            # The loop scheduler measures durable work and owns continuation.
            # It must receive every slice, including one with source evidence
            # but no newly completed review-queue items.
            if exhausted.kind == 'soft_deadline' or not remaining() or probe_only or batch_handoff:
                yield exhausted
                return
            trigger = "assistance_requested" if explicit_request else "segment_exhausted"
            if controller is not None:
                choice = await controller(trigger=trigger, made_progress=bool(current.get("advanced")),
                    identity=["iteration", state["slice_exhaustions"]])
            else:
                # Standalone consumers persist the same ledger in their state.
                record = state.setdefault("controller_record", {"node_states": {node_id: {}}, "node_ledgers": {}})
                choice = decide_and_record(record, node_id, trigger=trigger,
                    made_progress=bool(current.get("advanced")), identity=str(state["slice_exhaustions"]))
            state["controller_decision"] = asdict(choice)
            state.setdefault("decisions", []).append({"controller": asdict(choice), "execution_status": "待执行"})
            if choice.action not in {"progress_continue", "self_diagnose"}:
                state["phase"] = "yielding"
                await checkpoint()
                yield exhausted
                return
            state["phase"] = "continuing" if choice.action == "progress_continue" else "diagnosing"
            agent.max_iterations = original_limit
            reminder = ("从账本记录的上次进度继续未完成项。" if choice.action == "progress_continue" else
                "先读取错误和当前账本，用另一种方法诊断一次；需要额外能力时调用 RequestAssistance。")
            conversation.add_system_reminder(reminder)
            await checkpoint()
            await emit({"type": "expert_step", "node_id": node_id, "role": role,
                "status": "running", "label": "自动接续" if choice.action == "progress_continue" else "节点自诊",
                "summary": reminder, "recovery_phase": state["phase"], "controller": asdict(choice)})
    finally:
        agent.max_iterations = original_limit
