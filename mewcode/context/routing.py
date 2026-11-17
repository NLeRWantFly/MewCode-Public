"""Pluggable model-aware context routing.

The context manager historically had one provider-specific branch for
DeepSeek.  This module keeps the provider-independent decisions in immutable
policies and lets a runtime plugin select one policy for each model request.
The router is deliberately small: a policy is just a matcher plus data, and
custom policies can be contributed through the runtime slot without changing
the Agent or the context manager.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from mewcode.cordis import PluginSpec


CONTEXT_ROUTER_SERVICE = "context_router"
CONTEXT_POLICY_SLOT = "core.context.policies"
CONTEXT_ROUTER = CONTEXT_ROUTER_SERVICE
CONTEXT_POLICIES_SLOT = CONTEXT_POLICY_SLOT

_Matcher = Callable[["ContextRouteRequest"], bool]


@dataclass(frozen=True)
class ContextRouteRequest:
    """The stable, provider-neutral input used by :class:`ContextRouter`."""

    model: str = ""
    protocol: str = ""
    base_url: str = ""
    # ``auto`` is useful to callers that do not have model capabilities yet;
    # an explicit channel still takes precedence over model-name matching.
    context_channel: str = "auto"
    capabilities: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def canonical_model(self) -> str:
        value = str(self.model or "").strip().lower()
        # Keep routing independent from the pricing/capability registry while
        # handling the common provider/model and provider:model spellings.
        value = value.rsplit("/", 1)[-1]
        if ":" in value:
            parts = value.split(":")
            known_prefixes = ("deepseek-", "gpt-", "gpt5", "claude-", "gemini-")
            value = next(
                (part for part in parts if part.startswith(known_prefixes)),
                parts[-1],
            )
        if value.endswith("-latest"):
            value = value[:-len("-latest")]
        return value

    @property
    def model_name(self) -> str:
        """Alias used by integrations that call the request field model_name."""

        return self.canonical_model


@dataclass(frozen=True)
class ContextPolicy:
    """Immutable context behavior selected for a model request.

    ``keep_recent_ratio`` and its bounds are used for context windows whose
    compact tail is proportional to the window (currently DeepSeek).  Fixed
    token values are used by policies with a provider-stable compact budget.
    ``output_reserve`` means that the Agent should add the client's advertised
    output allowance when evaluating the compact threshold.
    """

    name: str
    priority: int = 0
    matcher: _Matcher | str | re.Pattern[str] | None = None
    context_channel: str = "generic"
    stable_system: bool = False
    stable_tools: bool = False
    move_hook_prompts_to_history: bool = False
    compact_trigger_ratio: float | None = None
    compact_trigger_tokens: int | None = None
    keep_recent_tokens: int | None = None
    keep_max_tokens: int | None = None
    keep_recent_ratio: float | None = None
    keep_recent_min_tokens: int = 0
    keep_recent_cap_tokens: int = 0
    post_compact_ratio: float | None = None
    compute_fixed_prefix: bool = False
    output_reserve: bool = False
    output_reserve_tokens: int = 0
    effort_affects_cache_affinity: bool = False

    def matches(self, request: ContextRouteRequest) -> bool:
        matcher = self.matcher
        if matcher is None:
            return True
        if callable(matcher):
            try:
                return bool(matcher(request))
            except (AttributeError, TypeError):
                # A small compatibility affordance for extensions whose
                # matcher predates ContextRouteRequest and accepts a model
                # string instead.
                return bool(matcher(request.canonical_model))
        if hasattr(matcher, "search"):
            return bool(matcher.search(request.model))
        return bool(re.search(str(matcher), request.model, re.IGNORECASE))

    # Small aliases keep policy consumers readable and make the extension
    # contract tolerant of the names used by older adapters.
    @property
    def stable_system_prompt(self) -> bool:
        return self.stable_system

    @property
    def freeze_system(self) -> bool:
        return self.stable_system

    @property
    def channel(self) -> str:
        return self.context_channel

    @property
    def stable_tool_schemas(self) -> bool:
        return self.stable_tools

    @property
    def freeze_tools(self) -> bool:
        return self.stable_tools

    @property
    def hook_prompts_in_history(self) -> bool:
        return self.move_hook_prompts_to_history

    @property
    def hook_prompts_to_history(self) -> bool:
        return self.move_hook_prompts_to_history

    @property
    def fixed_prefix(self) -> bool:
        return self.compute_fixed_prefix

    @property
    def calculate_fixed_prefix(self) -> bool:
        return self.compute_fixed_prefix

    @property
    def compact_keep_recent_tokens(self) -> int | None:
        return self.keep_recent_tokens

    @property
    def compact_keep_max_tokens(self) -> int | None:
        return self.keep_max_tokens

    @property
    def output_reserve_enabled(self) -> bool:
        return self.reserves_output_tokens

    @property
    def effort_changes_cache_affinity(self) -> bool:
        return self.effort_affects_cache_affinity

    @property
    def cache_affinity_on_effort_change(self) -> bool:
        return self.effort_affects_cache_affinity

    @property
    def reserves_output_tokens(self) -> bool:
        return self.output_reserve or self.output_reserve_tokens > 0


def _is_deepseek(request: ContextRouteRequest) -> bool:
    if request.context_channel == "deepseek":
        return True
    if request.context_channel == "generic":
        return False
    capabilities = request.capabilities
    if capabilities is not None:
        # A capabilities object records explicit ``context_channel=generic``
        # overrides, so do not re-detect DeepSeek from its model name.
        return getattr(capabilities, "context_channel", "") == "deepseek"
    return request.canonical_model.startswith("deepseek-")


def _is_gpt_56(request: ContextRouteRequest) -> bool:
    model = request.canonical_model
    return bool(re.search(r"(?:^|[-_])gpt[-_]?5[.]6(?:[.\-_]|$)", model))


def _is_astra(request: ContextRouteRequest) -> bool:
    return bool(re.search(r"(?:^|[-_])gpt-6-astra(?:-|$)", request.canonical_model))


def _is_gpt_6_sol_or_luna(request: ContextRouteRequest) -> bool:
    return bool(re.search(r"(?:^|[-_])gpt-(?:6-(?:sol|luna)|6\.1-sol)(?:-|$)", request.canonical_model))


def _is_claude(request: ContextRouteRequest) -> bool:
    model = request.canonical_model
    return model.startswith("claude-") or "anthropic" in model


def _is_gemini(request: ContextRouteRequest) -> bool:
    model = request.canonical_model
    return model.startswith("gemini-") or "gemini" in model


# Built-in policies intentionally contain no pricing information.  The
# generic policy is a real fallback rather than a special case in the Agent.
GENERIC_CONTEXT_POLICY = ContextPolicy(
    name="generic",
    priority=-1_000,
    matcher=lambda _request: True,
    context_channel="generic",
    stable_system=False,
    stable_tools=False,
    compact_trigger_ratio=None,
    keep_recent_tokens=10_000,
    keep_max_tokens=40_000,
    compute_fixed_prefix=False,
    output_reserve=False,
    effort_affects_cache_affinity=False,
)

DEEPSEEK_CONTEXT_POLICY = ContextPolicy(
    name="deepseek",
    priority=400,
    matcher=_is_deepseek,
    context_channel="deepseek",
    stable_system=True,
    stable_tools=True,
    move_hook_prompts_to_history=True,
    compact_trigger_ratio=0.85,
    keep_recent_ratio=0.10,
    keep_recent_min_tokens=32_000,
    keep_recent_cap_tokens=96_000,
    post_compact_ratio=0.50,
    compute_fixed_prefix=True,
    output_reserve=True,
    effort_affects_cache_affinity=False,
)

OPENAI_GPT_56_CONTEXT_POLICY = ContextPolicy(
    name="openai-gpt-5.6",
    priority=300,
    matcher=_is_gpt_56,
    context_channel="generic",
    stable_system=True,
    stable_tools=True,
    move_hook_prompts_to_history=True,
    compact_trigger_ratio=0.85,
    keep_recent_tokens=16_000,
    keep_max_tokens=64_000,
    compute_fixed_prefix=True,
    output_reserve=False,
    effort_affects_cache_affinity=True,
)

OPENAI_ASTRA_CONTEXT_POLICY = ContextPolicy(
    name="openai-gpt-6-astra", priority=310, matcher=_is_astra,
    context_channel="generic", stable_system=True, stable_tools=True,
    move_hook_prompts_to_history=True, compact_trigger_ratio=0.85,
    keep_recent_tokens=16_000, keep_max_tokens=64_000,
    compute_fixed_prefix=True, output_reserve=True,
    effort_affects_cache_affinity=True,
)

OPENAI_GPT_6_CONTEXT_POLICY = ContextPolicy(
    name="openai-gpt-6", priority=305, matcher=_is_gpt_6_sol_or_luna,
    context_channel="generic", stable_system=True, stable_tools=True,
    move_hook_prompts_to_history=True, compact_trigger_ratio=0.85,
    keep_recent_tokens=16_000, keep_max_tokens=64_000,
    compute_fixed_prefix=True, output_reserve=True,
    effort_affects_cache_affinity=True,
)

ANTHROPIC_MODERN_CONTEXT_POLICY = ContextPolicy(
    name="anthropic-modern",
    priority=200,
    matcher=_is_claude,
    context_channel="generic",
    stable_system=True,
    stable_tools=True,
    move_hook_prompts_to_history=True,
    compact_trigger_ratio=0.80,
    keep_recent_tokens=12_000,
    keep_max_tokens=48_000,
    compute_fixed_prefix=True,
    output_reserve=False,
    # Anthropic documents effort as part of the prompt-cache key.  Switching
    # it starts a fresh affinity epoch even when the visible message prefix is
    # otherwise unchanged.
    effort_affects_cache_affinity=True,
)

GEMINI_LONG_CONTEXT_POLICY = ContextPolicy(
    name="gemini-long-context",
    priority=200,
    matcher=_is_gemini,
    context_channel="generic",
    stable_system=True,
    stable_tools=True,
    move_hook_prompts_to_history=True,
    compact_trigger_ratio=0.85,
    keep_recent_tokens=16_000,
    keep_max_tokens=64_000,
    compute_fixed_prefix=True,
    output_reserve=False,
    effort_affects_cache_affinity=False,
)


# Short aliases are useful for callers that treat the policy names as the
# public API, while the longer constants make their purpose unambiguous.
GENERIC_POLICY = GENERIC_CONTEXT_POLICY
DEEPSEEK_POLICY = DEEPSEEK_CONTEXT_POLICY
GENERIC = GENERIC_CONTEXT_POLICY
DEEPSEEK = DEEPSEEK_CONTEXT_POLICY
OPENAI_GPT_56_POLICY = OPENAI_GPT_56_CONTEXT_POLICY
OPENAI_GPT_56 = OPENAI_GPT_56_CONTEXT_POLICY
ANTHROPIC_MODERN_POLICY = ANTHROPIC_MODERN_CONTEXT_POLICY
OPENAI_GPT_5_6_CONTEXT_POLICY = OPENAI_GPT_56_CONTEXT_POLICY
OPENAI_GPT_5_6_POLICY = OPENAI_GPT_56_CONTEXT_POLICY
ANTHROPIC_MODERN = ANTHROPIC_MODERN_CONTEXT_POLICY
GEMINI_LONG_CONTEXT = GEMINI_LONG_CONTEXT_POLICY

BUILTIN_CONTEXT_POLICIES: tuple[ContextPolicy, ...] = (
    DEEPSEEK_CONTEXT_POLICY,
    OPENAI_ASTRA_CONTEXT_POLICY,
    OPENAI_GPT_6_CONTEXT_POLICY,
    OPENAI_GPT_56_CONTEXT_POLICY,
    ANTHROPIC_MODERN_CONTEXT_POLICY,
    GEMINI_LONG_CONTEXT_POLICY,
    GENERIC_CONTEXT_POLICY,
)
BUILTIN_POLICIES = BUILTIN_CONTEXT_POLICIES


class ContextRouter:
    """Resolve the highest-priority matching policy for a request."""

    def __init__(
        self,
        policies: Iterable[ContextPolicy] = (),
        *,
        include_builtins: bool = True,
        policy_source: Callable[[], Iterable[ContextPolicy]] | None = None,
    ) -> None:
        initial = list(BUILTIN_CONTEXT_POLICIES if include_builtins else ())
        initial.extend(policies)
        self._policies = initial
        self._policy_source = policy_source

    @property
    def policies(self) -> tuple[ContextPolicy, ...]:
        policies = list(self._policies)
        if self._policy_source is not None:
            try:
                policies.extend(self._policy_source())
            except Exception:
                # A disappearing optional slot must not make model routing
                # fatal; the local policy set still includes a fallback.
                pass
        return tuple(
            sorted(
                (policy for policy in policies if isinstance(policy, ContextPolicy)),
                key=lambda policy: policy.priority,
                reverse=True,
            )
        )

    def add_policy(self, policy: ContextPolicy) -> None:
        if not isinstance(policy, ContextPolicy):
            raise TypeError("context policies must be ContextPolicy instances")
        self._policies.append(policy)

    register = add_policy

    def resolve(
        self,
        request: ContextRouteRequest | str | None = None,
        **request_fields: Any,
    ) -> ContextPolicy:
        if request is None:
            request = ContextRouteRequest(**request_fields)
        elif isinstance(request, str):
            request = ContextRouteRequest(model=request, **request_fields)
        elif request_fields:
            raise TypeError("request fields cannot accompany ContextRouteRequest")
        if not isinstance(request, ContextRouteRequest):
            raise TypeError("request must be ContextRouteRequest or model string")
        for policy in self.policies:
            if policy.matches(request):
                return policy
        return GENERIC_CONTEXT_POLICY

    route = resolve

    def __call__(
        self, request: ContextRouteRequest | str | None = None, **kwargs: Any
    ) -> ContextPolicy:
        return self.resolve(request, **kwargs)


def context_router_plugin(
    policies: Iterable[ContextPolicy] = (),
    *,
    version: str = "1",
) -> PluginSpec:
    """Return the runtime plugin that provides the context router service.

    Built-ins are registered in the policy slot so another plugin can add a
    higher-priority policy without reaching into the router implementation.
    The service reads that slot at resolve time, so load order does not cause
    a stale policy snapshot.
    """

    supplied = tuple(policies)

    def apply(ctx: Any) -> None:
        ctx.slots.declare(CONTEXT_POLICY_SLOT, kind="list", owner=CONTEXT_ROUTER_SERVICE)
        for policy in (*BUILTIN_CONTEXT_POLICIES, *supplied):
            ctx.slots.register(
                CONTEXT_POLICY_SLOT,
                policy,
                item_key=policy.name,
                priority=policy.priority,
            )
        router = ContextRouter(
            include_builtins=False,
            policy_source=lambda: ctx.slots.resolve(CONTEXT_POLICY_SLOT) or (),
        )
        ctx.provide(CONTEXT_ROUTER_SERVICE, router, replace=True)

    return PluginSpec(
        name=CONTEXT_ROUTER_SERVICE,
        apply_fn=apply,
        version=version,
        purpose="Model-aware context compression and cache-affinity routing",
        source="builtin",
    )


__all__ = [
    "ANTHROPIC_MODERN_CONTEXT_POLICY",
    "ANTHROPIC_MODERN_POLICY",
    "BUILTIN_CONTEXT_POLICIES",
    "BUILTIN_POLICIES",
    "CONTEXT_POLICY_SLOT",
    "CONTEXT_POLICIES_SLOT",
    "CONTEXT_ROUTER",
    "CONTEXT_ROUTER_SERVICE",
    "ContextPolicy",
    "ContextRouteRequest",
    "ContextRouter",
    "DEEPSEEK_CONTEXT_POLICY",
    "DEEPSEEK_POLICY",
    "DEEPSEEK",
    "GEMINI_LONG_CONTEXT_POLICY",
    "GENERIC_CONTEXT_POLICY",
    "GENERIC_POLICY",
    "GENERIC",
    "OPENAI_ASTRA_CONTEXT_POLICY",
    "OPENAI_GPT_6_CONTEXT_POLICY",
    "OPENAI_GPT_56_CONTEXT_POLICY",
    "OPENAI_GPT_56_POLICY",
    "OPENAI_GPT_56",
    "OPENAI_GPT_5_6_CONTEXT_POLICY",
    "OPENAI_GPT_5_6_POLICY",
    "ANTHROPIC_MODERN",
    "GEMINI_LONG_CONTEXT",
    "context_router_plugin",
]
