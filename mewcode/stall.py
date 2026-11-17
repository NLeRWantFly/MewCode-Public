"""Behavior-level stall signals; artifact-level audit convergence stays separate."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class StallPolicy:
    repeated_calls: int = 3
    consecutive_errors: int = 5
    stale_rounds: int = 8
    repeated_failed_commands: int = 3

    @classmethod
    def from_value(cls, value):
        if value is None or value is False:
            return None
        if isinstance(value, cls):
            return value
        values = value if isinstance(value, dict) else {}
        parsed = {key: int(values.get(key, default)) for key, default in cls().__dict__.items()}
        if any(number < 2 or number > 100 for number in parsed.values()):
            raise ValueError("Stall thresholds must be between 2 and 100")
        return cls(**parsed)


class StallDetector:
    def __init__(self, policy: StallPolicy, *, executor=False):
        self.policy, self.executor = policy, executor
        self.warned = False
        self.seen = set()
        self.reset()

    def reset(self):
        self.previous = None
        self.repeated = self.errors = self.idle_rounds = 0
        self.failed_commands = {}
        self.round_progress = self.round_blocked = False

    def trigger(self, reason):
        action = "stop" if self.warned else "warn"
        self.warned = True
        self.reset()
        return action, reason

    def observe_tool(self, name, arguments, *, is_error, read_only, operation_blocked=False):
        if operation_blocked:
            # Permission/review waits are local outcomes, not execution errors.
            # Their repeated attempts are bounded by the operation recovery.
            self.round_blocked = True
            return None
        if name in {"report_step", "AskUserQuestion", "human_oncall", "SubmitHandoff", "SubmitVerdict", "SubmitLoopDecision"}:
            return None
        signature = hashlib.sha256(json.dumps([name, arguments], sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        self.repeated = self.repeated + 1 if signature == self.previous else 1
        self.previous = signature
        self.errors = self.errors + 1 if is_error else 0
        if not is_error and (not read_only or signature not in self.seen):
            self.round_progress = True
        self.seen.add(signature)
        if name == "Bash" and is_error:
            self.failed_commands[signature] = self.failed_commands.get(signature, 0) + 1
            if self.failed_commands[signature] >= self.policy.repeated_failed_commands:
                return self.trigger("同一 Bash 命令反复失败")
        if self.repeated >= self.policy.repeated_calls:
            return self.trigger("连续工具调用及参数完全相同")
        if self.errors >= self.policy.consecutive_errors:
            return self.trigger("工具连续返回错误")
        return None

    def end_round(self):
        if self.round_blocked and not self.round_progress:
            self.round_blocked = False
            return None
        self.idle_rounds = 0 if self.round_progress else self.idle_rounds + 1
        self.round_progress = self.round_blocked = False
        if self.executor and self.idle_rounds >= self.policy.stale_rounds:
            return self.trigger("执行节点多轮没有成功写入或新的只读目标")
        return None


def stall_reminder(reason):
    return f"检测到重复/无进展：{reason}。请换一种方法；若无法推进，直接报告阻塞并结束，不要重复同类调用。"
