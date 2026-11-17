"""Receipt-driven token windows inside a separately authorized total ceiling.

The window is a scheduling lease, not a grant. Only durable host progress may
acknowledge work; missing samples never justify growth. No model assertion is
accepted as progress, and a ceiling cannot be changed here.
"""
from __future__ import annotations

import math
import statistics
import time
from typing import Any


POLICY_VERSION = "loop-token-window-v1"
MAX_WINDOW_RECEIPTS = 128


def initial_token_window(*, spent_tokens: int, floor: int, ceiling: int,
                         rate_per_minute: int | None = None,
                         burst_tokens: int | None = None,
                         now: float | None = None) -> dict[str, Any]:
    if (any(isinstance(value, bool) or not isinstance(value, int) for value in
            (spent_tokens, floor, ceiling)) or spent_tokens < 0 or floor < 1024
            or ceiling <= spent_tokens or floor > ceiling - spent_tokens):
        raise ValueError("invalid adaptive token window")
    bucket = None
    if rate_per_minute is not None:
        burst = burst_tokens if burst_tokens is not None else floor
        if (type(rate_per_minute) is not int or type(burst) is not int
                or not 1 <= rate_per_minute <= ceiling * 60
                or not 1024 <= burst <= ceiling):
            raise ValueError("invalid token bucket rate or burst")
        at = time.time() if now is None else now
        if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at) or at < 0:
            raise ValueError("invalid token bucket clock")
        bucket = {"rate_per_minute": rate_per_minute, "burst_tokens": burst,
                  "available": float(burst), "last_refill_at": float(at),
                  "last_settled_tokens": spent_tokens}
    elif burst_tokens is not None:
        raise ValueError("token bucket burst requires a configured rate")
    state = {"policy_version": POLICY_VERSION, "floor": floor, "ceiling": ceiling,
            "cwnd": floor, "ssthresh": ceiling, "issued_until": spent_tokens + floor,
            "last_spent_tokens": spent_tokens, "no_progress_streak": 0,
            "productive_costs": [], "last_segment_id": "", "last_receipt": None}
    if bucket is not None:
        state["token_bucket"] = bucket
    return state


def _validated_bucket(state: dict[str, Any], *, spent_tokens: int) -> dict[str, Any] | None:
    # Windows minted before the rate limiter was installed retain their grant
    # but do not silently acquire a new scheduling policy mid-run.
    bucket = state.get("token_bucket")
    if bucket is None:
        return None
    if not isinstance(bucket, dict):
        raise ValueError("invalid token bucket receipt")
    rate, burst = bucket.get("rate_per_minute"), bucket.get("burst_tokens")
    available, at, settled = (bucket.get("available"), bucket.get("last_refill_at"),
                               bucket.get("last_settled_tokens"))
    if (type(rate) is not int or not 1 <= rate <= state["ceiling"] * 60
            or type(burst) is not int
            or not 1024 <= burst <= state["ceiling"]
            or isinstance(available, bool) or not isinstance(available, (int, float))
            or not math.isfinite(available) or available > burst
            or isinstance(at, bool) or not isinstance(at, (int, float))
            or not math.isfinite(at) or at < 0
            or type(settled) is not int or not 0 <= settled <= spent_tokens):
        raise ValueError("invalid token bucket receipt")
    return bucket


def token_bucket_delay(state: dict[str, Any], *, spent_tokens: int,
                       now: float | None = None,
                       charge_unsettled: bool = False) -> tuple[dict[str, Any], float]:
    """Refill a durable bucket and return a queue delay before the next slice.

    Usage is only known after a provider call. A slice may exceed the burst;
    the resulting debt delays *future* slices, never erases measured usage.
    """
    validate_token_window(state, ceiling=state.get("ceiling"), spent_tokens=spent_tokens)
    bucket = _validated_bucket(state, spent_tokens=spent_tokens)
    if bucket is None:
        return dict(state), 0.0
    at = time.time() if now is None else now
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at) or at < 0:
        raise ValueError("invalid token bucket clock")
    if type(charge_unsettled) is not bool:
        raise ValueError("invalid token bucket charge mode")
    elapsed = max(0.0, float(at) - float(bucket["last_refill_at"]))
    available = min(float(bucket["burst_tokens"]), float(bucket["available"])
                    + elapsed * int(bucket["rate_per_minute"]) / 60.0)
    settled = int(bucket["last_settled_tokens"])
    if charge_unsettled:
        available -= spent_tokens - settled
        settled = spent_tokens
    updated = dict(state)
    updated["token_bucket"] = {**bucket, "available": available,
                               "last_refill_at": max(float(at), float(bucket["last_refill_at"])),
                               "last_settled_tokens": settled}
    # Zero balance is enough to dispatch one measured slice. Never reserve an
    # estimated token count as if it were actual usage.
    delay = max(0.0, -available * 60.0 / int(bucket["rate_per_minute"]))
    return updated, delay


def validate_token_window(state: dict[str, Any], *, ceiling: int, spent_tokens: int) -> None:
    if not isinstance(state, dict) or state.get("policy_version") != POLICY_VERSION:
        raise ValueError("adaptive token window receipt is missing")
    values = (state.get("floor"), state.get("ceiling"), state.get("cwnd"),
              state.get("ssthresh"), state.get("issued_until"),
              state.get("last_spent_tokens"), state.get("no_progress_streak"))
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise ValueError("adaptive token window receipt is invalid")
    floor, stored_ceiling, cwnd, threshold, issued, last_spent, streak = values
    costs = state.get("productive_costs")
    if (stored_ceiling != ceiling or floor < 1024 or floor > ceiling
            or not floor <= cwnd <= ceiling or not floor <= threshold <= ceiling
            or not 0 <= last_spent <= spent_tokens
            or not 0 <= issued <= ceiling or streak < 0
            or not isinstance(costs, list) or len(costs) > 8
            or any(isinstance(cost, bool) or not isinstance(cost, int) or cost < 1
                   for cost in costs)):
        raise ValueError("adaptive token window receipt is inconsistent")
    _validated_bucket(state, spent_tokens=spent_tokens)


def advance_token_window(state: dict[str, Any], *, segment_id: str,
                         spent_tokens: int, durable_progress: bool,
                         thrash: bool = False,
                         progress_items: int = 0,
                         remaining_items: int | None = None,
                         median_tokens_per_item: int | None = None,
                         now: float | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Settle one real segment and issue at most one next window.

    A duplicate segment ID returns the original receipt. A second unproductive
    segment with actual token spend stops issuing leases and requests replanning.
    The caller persists both state and receipt before dispatching more work.
    """
    if not isinstance(segment_id, str) or not segment_id:
        raise ValueError("segment_id is required")
    if isinstance(spent_tokens, bool) or not isinstance(spent_tokens, int):
        raise ValueError("spent_tokens must be an integer")
    validate_token_window(state, ceiling=state.get("ceiling"), spent_tokens=spent_tokens)
    if state.get("last_segment_id") == segment_id:
        prior = state.get("last_receipt") or {}
        if (prior.get("spent_tokens") != spent_tokens
                or prior.get("durable_progress") != durable_progress
                or prior.get("thrash") != thrash
                or prior.get("progress_items") != progress_items
                or prior.get("remaining_items") != remaining_items):
            raise ValueError("duplicate segment has conflicting token or progress receipt")
        return dict(state), dict(state["last_receipt"])
    if not isinstance(durable_progress, bool) or not isinstance(thrash, bool):
        raise ValueError("progress and thrash must be host booleans")
    if (isinstance(progress_items, bool) or not isinstance(progress_items, int)
            or progress_items < 0 or
            (remaining_items is not None and
             (isinstance(remaining_items, bool) or not isinstance(remaining_items, int)
              or remaining_items < 0)) or
            (median_tokens_per_item is not None and
             (isinstance(median_tokens_per_item, bool)
              or not isinstance(median_tokens_per_item, int)
              or median_tokens_per_item < 1))):
        raise ValueError("forecast requires nonnegative items and positive observed cost")
    before = int(state["cwnd"])
    floor, ceiling = int(state["floor"]), int(state["ceiling"])
    streak = int(state["no_progress_streak"])
    consumed = spent_tokens - int(state["last_spent_tokens"])
    if consumed < 0:
        raise ValueError("token usage cannot move backwards")
    next_state = dict(state)
    bucket = _validated_bucket(state, spent_tokens=spent_tokens)
    bucket_wait = 0.0
    if bucket is not None:
        replenished, _ = token_bucket_delay(state, spent_tokens=spent_tokens, now=now)
        bucket = replenished["token_bucket"]
        usage = spent_tokens - int(bucket["last_settled_tokens"])
        bucket = {**bucket, "available": float(bucket["available"]) - usage,
                  "last_settled_tokens": spent_tokens}
        bucket_wait = max(0.0, -float(bucket["available"]) * 60.0
                          / int(bucket["rate_per_minute"]))
        next_state["token_bucket"] = bucket
    costs = list(state["productive_costs"])
    if durable_progress and progress_items > 0 and consumed > 0:
        costs.append(math.ceil(consumed / progress_items))
        costs = costs[-8:]
    next_state["productive_costs"] = costs
    if durable_progress and not thrash:
        streak = 0
        growth = before if before < state["ssthresh"] else floor
        next_window = min(ceiling, before + growth, before * 2)
        signal = "durable_ack"
        action = "continue"
    elif consumed > 0:
        streak += 1
        next_state["ssthresh"] = max(floor, before // 2)
        next_window = floor if streak >= 2 else max(floor, before // 2)
        signal = "no_progress_timeout" if streak >= 2 else "loss_probe"
        action = "replan" if streak >= 2 else "continue"
    else:
        # An empty/cancelled segment is not a congestion sample. It cannot
        # increase the window or consume a failure strike.
        next_window = before
        signal, action = "no_sample", "continue"
    if spent_tokens >= ceiling:
        action = "reconcile_overrun" if spent_tokens > ceiling else "request_total_cap"
        issued = ceiling
    elif action == "replan":
        issued = spent_tokens
    elif signal == "loss_probe":
        issued = min(ceiling, spent_tokens + next_window)
    else:
        issued = min(ceiling, max(int(state["issued_until"]), spent_tokens + next_window))
    forecast = None
    if remaining_items is not None:
        rate = median_tokens_per_item or (math.ceil(statistics.median(costs)) if costs else None)
        if rate is not None:
            forecast = math.ceil(remaining_items * rate * 1.25)
    receipt = {"policy_version": POLICY_VERSION, "segment_id": segment_id,
               "signal": signal, "action": action,
               "spent_tokens": spent_tokens, "segment_tokens": consumed,
               "durable_progress": durable_progress, "thrash": thrash,
               "previous_cwnd": before, "next_cwnd": next_window,
               "issued_until": issued, "ceiling": ceiling,
               "remaining_items": remaining_items,
               "progress_items": progress_items,
               "productive_sample_count": len(costs),
               "forecast_remaining_tokens": forecast,
               "near_ceiling": spent_tokens * 5 >= ceiling * 4,
               "token_bucket_wait_seconds": round(bucket_wait, 3),
               "token_bucket_rate_per_minute": bucket["rate_per_minute"] if bucket else None,
               "upward_warning": ("approaching_authorized_ceiling"
                                  if spent_tokens * 5 >= ceiling * 4 and action == "continue"
                                  else None),
               "increases_total_budget": False, "changes_contract": False}
    next_state.update(cwnd=next_window, issued_until=issued,
                      last_spent_tokens=spent_tokens, no_progress_streak=streak,
                      last_segment_id=segment_id, last_receipt=receipt)
    return next_state, receipt


def append_window_change(snapshot: dict[str, Any], *, loop_state_key: str,
                         loop_id: str, loop_state: dict[str, Any]) -> bool:
    """Mirror the latest loop-local settlement into the run audit ledger once."""
    receipts = loop_state.get("token_window_receipts") or []
    if not receipts:
        return False
    if not isinstance(receipts, list) or not isinstance(receipts[-1], dict):
        raise ValueError("invalid token window audit receipt")
    policy = loop_state.get("loop_resource_policy") or {}
    receipt = receipts[-1]
    if (policy.get("mode") != "adaptive"
            or receipt.get("policy_version") != POLICY_VERSION
            or not isinstance(receipt.get("segment_id"), str)
            or not receipt["segment_id"]
            or receipt.get("ceiling") != policy.get("limit")
            or receipt.get("increases_total_budget") is not False
            or receipt.get("changes_contract") is not False):
        raise ValueError("invalid token window audit receipt")
    change_id = "window-" + loop_state_key + "-" + receipt["segment_id"]
    change = {**receipt, "change_id": change_id, "loop_id": loop_id,
              "loop_state_key": loop_state_key, "kind": "adaptive_window_settlement"}
    changes = snapshot.setdefault("loop_budget_changes", [])
    previous = next((row for row in changes if row.get("change_id") == change_id), None)
    if previous is not None:
        if previous != change:
            raise ValueError("conflicting token window audit receipt")
        return False
    changes.append(change)
    return True
