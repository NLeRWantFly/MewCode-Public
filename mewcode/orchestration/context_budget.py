"""Per-request collaboration capacity from the actual bound model catalog.

This is an input-window estimate, never a grant of execution or spending quota.
The ordinary Agent still measures its full system/tools/history before sending.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

from mewcode.model_registry import ModelRef


def estimate_text_tokens(text):
    """Conservative mixed-language estimate without pretending chars are tokens.

    ASCII uses three chars/token, CJK two tokens/char, other Unicode its UTF-8
    byte count. A separate window margin covers tokenizer/protocol differences.
    This is not provider usage accounting or an exact tokenizer result.
    """
    ascii_count = 0
    other = 0
    for char in text:
        code = ord(char)
        if code < 128:
            ascii_count += 1
        elif 0x2E80 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF:
            other += 2
        else:
            other += len(char.encode('utf-8'))
    return math.ceil(ascii_count / 3) + other


@dataclass(frozen=True)
class ContextBudget:
    limit: int
    unit: str = 'characters'
    model_id: str = ''
    window: int = 0
    output_reserve: int = 0
    host_reserve: int = 0
    safety_margin: int = 0

    @property
    def char_budget(self):
        # Only an allocation/search bound. fits() always measures actual text.
        return self.limit * 3 if self.unit == 'estimated_tokens' else self.limit

    def measure(self, text):
        return estimate_text_tokens(text) if self.unit == 'estimated_tokens' else len(text)

    def fits(self, text):
        return self.measure(text) <= self.limit

    def subtract(self, text='', *, reserve=0):
        return replace(self, limit=self.limit - self.measure(text) - reserve)

    def details(self):
        return dict(unit=self.unit, available=self.limit, model_id=self.model_id,
                    context_window_tokens=self.window, output_reserve_tokens=self.output_reserve,
                    host_reserve_tokens=self.host_reserve, safety_margin_tokens=self.safety_margin)

    def exceeded(self):
        if self.unit == 'characters':
            return f'必需协作上下文自身已超过 {self.limit} 字符预算，无法在预算内完整交付。'
        return (f'必需协作上下文超过 {self.model_id} 本次可用的 {self.limit:,} Token 估算容量'
                f'（模型窗口 {self.window:,} Token，已预留输出、宿主提示与安全余量）；'
                '完整要求已保留，需要继续拆分上下文。')


def as_budget(value):
    if isinstance(value, ContextBudget):
        return value
    if type(value) is not int or value <= 0:
        raise ValueError('协作策略必须给出正整数 context_char_budget 备用预算。')
    return ContextBudget(value)


def model_context_budget(pool, model, policy, *, system='', tools=False):
    ref = ModelRef.from_value(model)
    row = pool.rows.get((ref.provider_key, ref.model_id), {})
    window = row.get('context_window')
    if type(window) is not int or window <= 0:
        # Old imports and minimal/offline catalogs retain their explicit fallback.
        return as_budget(policy.get('context_char_budget'))
    output = row.get('request_output_tokens')
    if type(output) is not int or output <= 0:
        output = row.get('max_output_tokens')
    if type(output) is not int or output <= 0:
        output = min(8192, window // 4)
    margin = max(1024, math.ceil(window * .1))
    host = min(16000, max(2048, window // 10)) if tools else 256
    available = window - output - margin - host - estimate_text_tokens(system)
    if available <= 0:
        raise ValueError(f'{ref.model_id} 的模型上下文窗口不足以容纳配置的输出预留与固定提示。')
    return ContextBudget(available, 'estimated_tokens', ref.model_id, window, output, host, margin)
