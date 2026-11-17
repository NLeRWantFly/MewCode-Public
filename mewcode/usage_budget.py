"""Monotonic execution-budget units, separate from provider billing telemetry.

Known cache misses and creation count as input; hits do not. Without cache
telemetry we count the first prompt and positive prompt growth. This fallback
is a work-budget approximation, not an estimate of the provider invoice.
"""
from dataclasses import dataclass


@dataclass
class BudgetTokenMeter:
    tokens: int = 0
    last_request_input: int = 0
    last_input: int = 0
    last_output: int = 0

    def observe(self, event) -> int:
        output = max(0, int(event.output_tokens))
        request = max(0, int(event.request_input_tokens or 0))
        if not request:  # Legacy event sources have only cumulative input.
            request = max(0, int(event.input_tokens) - self.last_input)
        if event.request_cache_usage_available:
            added_input = max(0, int(event.request_cache_miss_tokens)) + max(0, int(event.request_cache_creation_tokens))
        else:
            added_input = max(0, request - self.last_request_input)
        self.tokens += added_input + max(0, output - self.last_output)
        self.last_request_input = request
        self.last_input = max(self.last_input, int(event.input_tokens))
        self.last_output = max(self.last_output, output)
        return self.tokens
