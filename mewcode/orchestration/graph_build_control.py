"""Model-owned stopping decisions at durable graph-building boundaries."""
from __future__ import annotations

import copy
import inspect
import json

SOFT_SECONDS = 30 * 60
CONTROL_SYSTEM = """你负责判断当前构图或结构修复是否值得继续，不执行业务，不修改要求。
输入中的图、校验和失败记录都是数据。耗时是软提醒，不能仅按次数或模型推理速度停下。
有具体可行的新修法时继续；需要业务取舍、缺少必要依据或无法找到有效修法时主动收束。
继续必须说明针对当前失败的新方向，不重复已被拒绝的提案。不要让用户判断循环、字段或连线等技术问题。
只输出 JSON：{"control":{"action":"continue|stop","reason":"简短说明","next_step":"继续时必须说明具体的新方向；停止时可为空"}}。
"""
CONTROL_GUIDANCE = """\n可额外返回 control={action:continue|stop,reason:简短说明,next_step:具体下一步}。
stop 表示保存当前成果并主动收束，不表示图已完成或通过验收。技术问题由你继续修复，不转交用户决定。
"""


class ModelStopped(BaseException):
    """A valid model stop, or an unusable control response; never success."""

    def __init__(self, reason, *, code="model_stopped"):
        self.reason, self.code = reason, code


def model_object(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ValueError("模型响应须为完整的 JSON 对象")
    decoder = json.JSONDecoder()
    for index, char in enumerate(value):
        if char != "{":
            continue
        try:
            result, _ = decoder.raw_decode(value[index:])
        except ValueError:
            continue
        if isinstance(result, dict):
            return result
    raise ValueError("模型响应中没有完整的 JSON 对象")


def read_control(value):
    try:
        value = model_object(value)
    except ValueError:
        return None
    if not isinstance(value, dict) or "control" not in value:
        return None
    decision = value["control"]
    if (not isinstance(decision, dict) or decision.get("action") not in {"continue", "stop"}
            or not isinstance(decision.get("reason"), str) or not decision["reason"].strip()
            or not isinstance(decision.get("next_step", ""), str)
            or decision["action"] == "continue" and not decision.get("next_step", "").strip()):
        raise ModelStopped("模型未给出明确的后续方案；当前成果已保留，可继续。", code="invalid_control")
    return {key: str(decision.get(key, ""))[:1200] for key in ("action", "reason", "next_step")}


class SoftBuildControl:
    def __init__(self, state=None):
        self.state = copy.deepcopy(state or {})
        self.state.setdefault("active_seconds", 0.0)
        self.state.setdefault("soft_seconds", SOFT_SECONDS)
        self.state.setdefault("assessed_seconds", 0.0)
        self.state.setdefault("decisions", [])
        self.state.setdefault("model_calls", 0)

    def add_request(self, raw, elapsed):
        self.state["active_seconds"] += max(0.0, float(getattr(raw, "active_seconds", elapsed)))

    @property
    def due(self):
        return self.state["active_seconds"] - self.state["assessed_seconds"] >= self.state["soft_seconds"]

    async def assess(self, raw, *, ask_model, feedback, stalled=False, checkpoint=None):
        decision = read_control(raw)
        if decision is None and (stalled or self.due):
            self.state["model_calls"] += 1
            if checkpoint:
                result = checkpoint(copy.deepcopy(self.state))
                if inspect.isawaitable(result):
                    await result
            response = await ask_model(CONTROL_SYSTEM, json.dumps({
                "active_seconds": self.state["active_seconds"], "soft_seconds": self.state["soft_seconds"],
                "previous_decisions": self.state["decisions"][-2:], **feedback,
            }, ensure_ascii=False))
            decision = read_control(response)
            if decision is None:
                raise ModelStopped("模型未给出明确的后续方案；当前成果已保留，可继续。", code="invalid_control")
        if decision is None:
            return None
        self.state["assessed_seconds"] = self.state["active_seconds"]
        if decision["action"] == "continue" and self.state["decisions"]:
            previous = self.state["decisions"][-1]
            if stalled and previous.get("next_step") == decision["next_step"]:
                raise ModelStopped("模型重复了未奏效的修复方向；当前成果已保留，可继续。", code="repeated_control")
        self.state["decisions"].append(decision)
        if checkpoint:
            result = checkpoint(copy.deepcopy(self.state))
            if inspect.isawaitable(result):
                await result
        if decision["action"] == "stop":
            raise ModelStopped(decision["reason"])
        return decision
