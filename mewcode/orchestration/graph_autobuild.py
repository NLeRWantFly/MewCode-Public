"""Bounded, checkpointable construction of a source-linked candidate graph.

Extraction is performed by the existing source-cursor pipeline before this
state machine starts. No step here saves a canvas or starts an orchestration.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import time
from typing import Any, Callable

from .graph_autofix import autofix, locked_snapshot
from .graph_readiness import readiness
from .graph_review import gap_diagnostics, review_coverage
from .graph_build_control import CONTROL_GUIDANCE, ModelStopped, SoftBuildControl


MAX_REPAIR_ROUNDS = 3
MAX_MODEL_CALLS = 8
MAX_SECONDS = 30 * 60
TIME_LIMIT_REASON = "构建时长已达上限"
CALL_LIMIT_REASON = "构建模型调用已达上限"


class _DeadlineExpired(Exception):
    pass


class _CallBudgetExpired(BaseException):
    """Control-flow stop; model-error handlers must not swallow this limit."""


class _CheckpointFailed(BaseException):
    def __init__(self, original):
        self.original = original


def new_build_state(document, *, now: float | None = None) -> dict:
    """Create the durable cursor after the extraction checkpoint is complete."""
    started = time.time() if now is None else now
    return {
        "phase": "autofix", "status": "working", "reason": "",
        "started_at": started, "deadline_at": started + MAX_SECONDS,
        "model_calls": 0, "call_attempts": 0, "locked_baseline": locked_snapshot(document),
        "autofix": {"rounds": [], "model_calls": 0},
        "gap_fill": {"rounds": [], "model_calls": 0},
        "review": None, "final_review": None,
        "remaining": [], "pending_gaps": [],
    }


def restart_build_state(document, previous, *, now: float | None = None) -> dict:
    """Start an explicit new attempt on the same graph, retaining past receipts."""
    if previous.get("phase") != "done" or previous.get("status") == "ready":
        raise ValueError("只有已结束且需要审查的构图可重新修复；进行中的构图请继续或停止")
    state = new_build_state(document, now=now)
    if previous.get("control"):
        state["control"] = copy.deepcopy(previous["control"])
        state["control"]["model_calls"] = 0  # Prior calls remain in the prior attempt's receipt.
    history = copy.deepcopy(previous.get("previous_attempts", []))
    history.append({key: copy.deepcopy(value) for key, value in previous.items()
                    if key not in {"locked_baseline", "previous_attempts"}})
    state["previous_attempts"] = history[-3:]
    return state


async def run_autobuild(document, base, sources, *, conversation_id: str, revision: int,
                        ask_model: Callable[[str, str], Any],
                        ask_review: Callable[[str, str], Any],
                        state: dict | None = None,
                        on_checkpoint: Callable[[dict, dict], Any] | None = None,
                        clock: Callable[[], float] = time.time,
                        autonomous: bool = False, plan_only: bool = False,
                        review_context: dict | None = None) -> dict:
    """Resume completion, independent review, and at most one gap-fill round.

    A checkpoint receives detached copies of the state and document. It must
    persist both in one transaction before returning; callback failure stops
    the build so a later caller resumes from the last successful checkpoint.
    """
    current = copy.deepcopy(document)
    build = copy.deepcopy(state) if state is not None else new_build_state(current, now=clock())
    build["call_attempts"] = max(0, int(build.get("call_attempts") or build.get("model_calls") or 0))
    policy = SoftBuildControl(build.get("control"))
    last_raw = ""
    from .draft_dedupe import apply_dedupe
    current, host_changes, _ = apply_dedupe(current)
    if host_changes:
        build.setdefault("host_dedupe", []).extend(host_changes)
        for change in host_changes:
            if build["locked_baseline"].get(change["entry_id"]) == change["before"]:
                build["locked_baseline"][change["entry_id"]] = copy.deepcopy(change["after"])

    async def persist():
        if on_checkpoint is not None:
            result = on_checkpoint(copy.deepcopy(build), copy.deepcopy(current))
            if inspect.isawaitable(result):
                await result

    def remaining_time():
        return max(0.0, float(build["deadline_at"]) - clock())

    async def bounded(coro):
        if autonomous:
            return await coro
        remaining = remaining_time()
        if remaining <= 0:
            coro.close()
            raise _DeadlineExpired()
        task = asyncio.create_task(coro)
        try:
            return await asyncio.wait_for(task, timeout=remaining)
        except TimeoutError:
            if task.cancelled():
                raise _DeadlineExpired() from None
            raise

    async def budgeted(provider, system, user):
        # Reserve durably before dispatch. An interrupted/unknown request keeps
        # its slot, so repeated stop/restart cannot reset the cost ceiling.
        if not autonomous and build["call_attempts"] >= MAX_MODEL_CALLS:
            raise _CallBudgetExpired()
        build["call_attempts"] += 1
        try:
            await persist()
        except Exception as exc:
            raise _CheckpointFailed(exc) from exc
        started = time.monotonic()
        raw = await provider(system, user)
        if autonomous:
            policy.add_request(raw, time.monotonic() - started)
            build["control"] = copy.deepcopy(policy.state)
        return raw

    async def generate(system, user):
        nonlocal last_raw
        last_raw = await budgeted(ask_model, system + (CONTROL_GUIDANCE if autonomous else ""), user)
        return last_raw

    async def review_model(system, user):
        return await budgeted(ask_review, system, user)

    def call_count():
        return (int((build.get("control") or {}).get("model_calls", 0))
                + build.get("retired_extraction_calls", 0)
                + build.get("retired_review_calls", 0) + build.get("retired_gap_fill_calls", 0)
                + build["autofix"]["model_calls"] + build["gap_fill"]["model_calls"]
                + int(build["review"] is not None and build["review"]["model_calls"])
                + int(build["final_review"] is not None and build["final_review"]["model_calls"]))

    def update_count():
        build["model_calls"] = call_count()
        if not autonomous and build["model_calls"] > MAX_MODEL_CALLS:
            raise RuntimeError("构建模型调用已达上限")

    async def finish(status, reason="", *, gaps=None):
        final = readiness(current, base, conversation_id=conversation_id, revision=revision)
        from .planning_issues import pending_question_gate
        question_reason, deferred = pending_question_gate(current, plan_only=plan_only)
        from .graph_discussion import waiting_questions
        waits = waiting_questions(current)
        if status == "ready" and not final["ready"]:
            status, reason = "needs_review", "structural_not_ready"
        if status == "ready" and question_reason:
            status, reason = "needs_review", question_reason
        build.update(phase="done", status=status, reason=reason,
                     remaining=copy.deepcopy(final["diagnostics"]),
                     pending_gaps=copy.deepcopy(gaps or []),
                     deferred_capability_question_ids=sorted(deferred))
        build["remaining_active_seconds"] = remaining_time()
        if waits and status == "needs_review" and reason in {"blocking_goal", "pending_questions", "conflict_requires_review"}:
            build.update(phase="waiting_user", status="waiting_user", resume_phase="review",
                         waiting_since=clock(), reason="business_decision")
        update_count()
        await persist()

    async def assess_control(raw, feedback, stalled=False):
        async def save(control):
            build["control"] = control
            update_count()
            await persist()
        async def decide(system, user):
            return await budgeted(ask_model, system, user)
        await policy.assess(raw, ask_model=decide, stalled=stalled, feedback=feedback, checkpoint=save)

    async def assess_round(record):
        if not autonomous or (record.get("structural") or {}).get("reason") == "model_error":
            return
        # Advisory warnings remain visible but cannot alone send a structurally
        # ready graph into a stop decision before independent coverage review.
        stalled = bool(record.get("error")) or (record.get("blocking_diagnostics_after", record.get("diagnostics_after", 0)) > 0 and
            not any((record.get(p) or {}).get("adopted") for p in ("structural", "ledger")))
        await assess_control(last_raw, {"phase": build["phase"], "last_attempt": record}, stalled)

    if build.get("manual_edit_pending_review") and build.get("review_stale") and build.get("phase") in {
            "autofix", "review", "gap_fill", "final_review"}:
        # A normal manual edit invalidates diagnostics, not budgets or history.
        # Recheck the current structure before reviewing it; never spend a call
        # repairing gaps from the pre-edit document on a paused-build resume.
        previous_reviews = {key: copy.deepcopy(build[key]) for key in ("review", "final_review")
                            if build.get(key) is not None}
        if previous_reviews:
            build.setdefault("manual_edit_review_history", []).append(previous_reviews)
            build["retired_review_calls"] = build.get("retired_review_calls", 0) + sum(
                int(receipt.get("model_calls", 0)) for receipt in previous_reviews.values())
        build.update(phase="autofix", status="working", reason="", review=None,
                     final_review=None, pending_gaps=[], manual_edit_pending_review=False)
        update_count()
        await persist()

    if build.get("phase") in {"done", "waiting_user"}:
        if build.get("phase") == "done" and build.get("status") == "ready":
            await finish("ready", gaps=build.get("pending_gaps"))
        return {"document": current, "build": build}

    try:
        while build.get("phase") not in {"done", "waiting_user"}:
            if build["phase"] == "autofix":
                async def on_round(record, round_document, calls):
                    nonlocal current
                    current = copy.deepcopy(round_document)
                    build["autofix"]["rounds"].append(record)
                    build["autofix"]["model_calls"] = calls
                    update_count()
                    await persist()
                    await assess_round(record)

                result = await bounded(autofix(
                    current, base, conversation_id=conversation_id, revision=revision,
                    ask_model=generate, max_repair_rounds=None if autonomous else MAX_REPAIR_ROUNDS,
                    locked_baseline=build["locked_baseline"],
                    resume=build["autofix"], on_round=on_round,
                    previous_rounds=[row for attempt in build.get("previous_attempts", [])
                        for section in ("autofix", "gap_fill")
                        for row in (attempt.get(section) or {}).get("rounds", [])][-2:]))
                current = result["document"]
                build["autofix"] = {"rounds": result["rounds"], "model_calls": result["model_calls"]}
                update_count()
                if result["status"] != "ready" or result["reason"] in {"model_error", "request_too_large", "parse_failed"}:
                    await finish("needs_review", result["reason"] or "structural_not_ready")
                    return {"document": current, "build": build}
                build["phase"] = "review"
                await persist()
                from .graph_discussion import waiting_questions
                if any(row["blocking"] for row in waiting_questions(current)):
                    await finish("needs_review", "blocking_goal")
                    return {"document": current, "build": build}

            if build["phase"] == "review":
                review = await bounded(review_coverage(sources, current, ask_review=review_model,
                                                       failure_context=review_context))
                build["review"] = review
                build["review_stale"] = False
                update_count()
                if review["error"]:
                    build["failure"] = {**review["failure"], "phase": "review"}
                    await finish("needs_review", "review_error")
                    return {"document": current, "build": build}
                if not review["gaps"]:
                    await finish("ready")
                    return {"document": current, "build": build}
                # A fresh review has fresh diagnostics. An earlier completed gap
                # round cannot stand in for repairing these gaps after adoption.
                # Keep its receipts and cost; only its completion cursor is retired.
                previous_gap_fill = build["gap_fill"]
                if previous_gap_fill["rounds"] or previous_gap_fill["model_calls"]:
                    build.setdefault("gap_fill_history", []).append(copy.deepcopy(previous_gap_fill))
                    build["retired_gap_fill_calls"] = (build.get("retired_gap_fill_calls", 0)
                                                       + previous_gap_fill["model_calls"])
                    build["gap_fill"] = {"rounds": [], "model_calls": 0}
                build["phase"] = "gap_fill"
                build["pending_gaps"] = copy.deepcopy(review["gaps"])
                await persist()

            if build["phase"] == "gap_fill":
                diagnostics = gap_diagnostics(build["review"]["gaps"])

                async def on_gap_round(record, round_document, calls):
                    nonlocal current
                    current = copy.deepcopy(round_document)
                    build["gap_fill"]["rounds"].append(record)
                    build["gap_fill"]["model_calls"] = calls
                    update_count()
                    await persist()
                    await assess_round(record)

                result = await bounded(autofix(
                    current, base, conversation_id=conversation_id, revision=revision,
                    ask_model=generate, max_repair_rounds=0,
                    extra_diagnostics=diagnostics, require_improvement=False,
                    locked_baseline=build["locked_baseline"],
                    resume=build["gap_fill"], on_round=on_gap_round))
                current = result["document"]
                build["gap_fill"] = {"rounds": result["rounds"], "model_calls": result["model_calls"]}
                update_count()
                if result["status"] != "ready" or result["reason"] in {"model_error", "request_too_large", "parse_failed"}:
                    await finish("needs_review", result["reason"] or "structural_not_ready",
                                 gaps=build["review"]["gaps"])
                    return {"document": current, "build": build}
                build["phase"] = "final_review"
                await persist()

            if build["phase"] == "final_review":
                review = await bounded(review_coverage(sources, current, ask_review=review_model,
                                                       failure_context=review_context))
                build["final_review"] = review
                build["review_stale"] = False
                update_count()
                if review["error"]:
                    build["failure"] = {**review["failure"], "phase": "final_review"}
                    await finish("needs_review", "review_error", gaps=build["review"]["gaps"])
                elif review["gaps"]:
                    if not autonomous:
                        await finish("needs_review", "coverage_gaps_remaining", gaps=review["gaps"])
                    else:
                        await assess_control("", {"phase": "final_review", "pending_gaps": review["gaps"],
                            "previous_gaps": build["review"]["gaps"]}, review["gaps"] == build["review"]["gaps"])
                        build["retired_review_calls"] = build.get("retired_review_calls", 0) + build["review"]["model_calls"]
                        build.setdefault("gap_fill_history", []).append(copy.deepcopy(build["gap_fill"]))
                        build["retired_gap_fill_calls"] = build.get("retired_gap_fill_calls", 0) + build["gap_fill"]["model_calls"]
                        build.update(phase="gap_fill", review=review, final_review=None,
                                     gap_fill={"rounds": [], "model_calls": 0}, pending_gaps=copy.deepcopy(review["gaps"]))
                        update_count()
                        await persist()
                else:
                    await finish("ready")
    except _DeadlineExpired:
        await finish("needs_review", TIME_LIMIT_REASON, gaps=build.get("pending_gaps"))
    except _CallBudgetExpired:
        await finish("needs_review", CALL_LIMIT_REASON, gaps=build.get("pending_gaps"))
    except ModelStopped as exc:
        build["resume_phase"] = build["phase"]
        build["stop_summary"] = exc.reason
        await finish("needs_review", exc.code, gaps=build.get("pending_gaps"))
    except _CheckpointFailed as exc:
        raise exc.original

    return {"document": current, "build": build}
