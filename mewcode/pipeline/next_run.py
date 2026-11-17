"""Adaptive pipeline scheduling: decide the next one-shot fire time."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


MIN_DELAY = timedelta(seconds=60)
MAX_DELAY = timedelta(days=366)


@dataclass(frozen=True)
class NextRunDecision:
    next_fire_at: datetime
    reason: str
    source: str = "rule"


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _valid_future(value: datetime, now: datetime) -> bool:
    delay = _aware_utc(value) - _aware_utc(now)
    return MIN_DELAY <= delay <= MAX_DELAY


def _tomorrow_at(now: datetime, hour: int, minute: int = 0) -> datetime:
    local = _aware_utc(now).astimezone()
    target = (local + timedelta(days=1)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return target.astimezone(timezone.utc)


def decide_by_rule(
    policy: str,
    *,
    succeeded: bool,
    now: datetime | None = None,
) -> NextRunDecision | None:
    """Handle explicit retry/success policies without an LLM."""
    now = _aware_utc(now or datetime.now(timezone.utc))
    text = policy.strip().lower()
    branch = text
    if "失败" in text and "成功" in text:
        fail_pos, success_pos = text.find("失败"), text.find("成功")
        if succeeded:
            branch = text[success_pos:fail_pos] if success_pos < fail_pos else text[success_pos:]
        else:
            branch = text[fail_pos:success_pos] if fail_pos < success_pos else text[fail_pos:]
    elif "failure" in text and "success" in text:
        fail_pos, success_pos = text.find("failure"), text.find("success")
        if succeeded:
            branch = text[success_pos:fail_pos] if success_pos < fail_pos else text[success_pos:]
        else:
            branch = text[fail_pos:success_pos] if fail_pos < success_pos else text[fail_pos:]

    delay_patterns = (
        (r"(\d+)\s*(?:分钟|分)", "minutes"),
        (r"(\d+)\s*(?:小时|时)", "hours"),
        (r"(\d+)\s*(?:天)", "days"),
        (r"(\d+)\s*minutes?", "minutes"),
        (r"(\d+)\s*hours?", "hours"),
        (r"(\d+)\s*days?", "days"),
    )
    if "半小时" in branch or "half an hour" in branch:
        target = now + timedelta(minutes=30)
        return NextRunDecision(target, "按策略在 30 分钟后续跑")
    for pattern, unit in delay_patterns:
        match = re.search(pattern, branch)
        if match:
            target = now + timedelta(**{unit: int(match.group(1))})
            if _valid_future(target, now):
                return NextRunDecision(target, f"按策略在 {match.group(1)} {unit}后续跑")

    tomorrow = re.search(
        r"(?:明天|tomorrow)(?:\s*(?:at|上午|下午|晚上))?\s*(\d{1,2})(?:[:点时](\d{1,2})?)?",
        branch,
    )
    if tomorrow:
        hour = int(tomorrow.group(1))
        minute = int(tomorrow.group(2) or 0)
        if any(word in tomorrow.group(0) for word in ("下午", "晚上")) and hour < 12:
            hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return NextRunDecision(
                _tomorrow_at(now, hour, minute),
                f"按策略在明天 {hour:02d}:{minute:02d} 续跑",
            )
    return None


def build_next_run_prompt(
    policy: str,
    run_summary: dict[str, Any],
    *,
    now: datetime | None = None,
) -> str:
    now = _aware_utc(now or datetime.now(timezone.utc))
    return (
        "你是 pipeline 下一次运行时间决策器。根据策略和本轮结果，只输出 JSON，"
        "格式为 {\"next_fire_at\": \"带时区的 ISO 8601 时间\", \"reason\": \"简短理由\"}。\n"
        "next_fire_at 必须晚于当前时间至少 60 秒，且不超过 366 天。不要输出 markdown。\n"
        f"当前时间: {now.isoformat()}\n"
        f"策略: {policy}\n"
        f"本轮结果: {json.dumps(run_summary, ensure_ascii=False, default=str)}"
    )


def parse_next_run_reply(
    reply: str,
    *,
    now: datetime | None = None,
) -> NextRunDecision | None:
    now = _aware_utc(now or datetime.now(timezone.utc))
    match = re.search(r"\{.*\}", reply, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        value = datetime.fromisoformat(str(data.get("next_fire_at", "")).replace("Z", "+00:00"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    value = _aware_utc(value)
    if not _valid_future(value, now):
        return None
    reason = str(data.get("reason") or "由自适应策略决定")[:500]
    return NextRunDecision(value, reason, source="llm")
