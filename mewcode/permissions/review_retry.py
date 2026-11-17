"""Transport cooldown is not a safety verdict or permission grant."""
from __future__ import annotations

import hashlib
import json
import math

RATE_LIMITED = "safety_review_rate_limited"
MAX_COOLDOWN = 60.0
MAX_RECOVERIES = 2


def action_key(tool_name, arguments):
    return hashlib.sha256(json.dumps([tool_name, arguments], sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def retry_delay(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 2.0
    return max(1.0, seconds) if math.isfinite(seconds) and seconds >= 0 else 2.0


def retry_remaining(route, now):
    if route.get("failure_kind") != "rate_limit" or route.get("status") != "unavailable":
        return None
    try:
        deadline = float(route["retry_not_before"])
    except (KeyError, TypeError, ValueError):
        return None
    return max(0.0, deadline - now) if math.isfinite(deadline) else None
