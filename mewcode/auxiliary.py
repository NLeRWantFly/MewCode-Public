"""Shared routing for low-cost auxiliary LLM calls.

Compaction, memory extraction, session summaries, and similar housekeeping
should not silently inherit an expensive primary model. Pipeline already has
the heavy/light tier policy, so this module reuses that policy for the rest of
the application.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Callable

from mewcode.client import LLMClient, create_client, model_effort_levels
from mewcode.config import ProviderConfig
from mewcode.model_capabilities import resolve_model_capabilities
from mewcode.opencode_go import resolve_effective_provider_config
from mewcode.pipeline.auto_policy import converge_effort, pick_tier_model
from mewcode.pipeline.models import MODEL_TIER_LIGHT

log = logging.getLogger(__name__)


def create_auxiliary_client(
    provider: ProviderConfig,
    config: Any | None = None,
    *,
    client_factory: Callable[[ProviderConfig], LLMClient] = create_client,
) -> LLMClient | None:
    """Create an isolated client for cheap background/housekeeping calls.

    The configured ``pipeline.auto_light_model`` wins. When no light model is
    configured or discoverable, routing falls back to the current session
    model but still uses the lightest supported reasoning effort. A separate
    client is always created so changing its effort cannot mutate the primary
    chat client.

    Client construction is best-effort: callers can safely fall back to their
    primary client when ``None`` is returned.
    """

    model, reason = pick_tier_model(
        MODEL_TIER_LIGHT,
        config,
        [provider.model],
        session_model=provider.model,
    )
    target_model = model or provider.model
    auxiliary_provider = copy.deepcopy(provider)
    auxiliary_provider.model = target_model
    auxiliary_provider._fetched_context_window = 0

    try:
        auxiliary_provider = resolve_effective_provider_config(auxiliary_provider)
        client = client_factory(auxiliary_provider)
    except Exception as exc:
        log.warning(
            "Unable to create auxiliary client for model %s; using primary client: %s",
            target_model,
            exc,
        )
        return None

    available = [{
        "id": target_model,
        "efforts": model_effort_levels(auxiliary_provider.protocol, target_model),
    }]
    capabilities = resolve_model_capabilities(
        target_model,
        auxiliary_provider.base_url,
        auxiliary_provider.context_channel,
    )
    target_effort = "off" if capabilities.context_channel == "deepseek" else "low"
    effort = converge_effort(
        target_effort, target_model, available, protocol=auxiliary_provider.protocol
    )
    if effort:
        client.set_effort(effort)
    log.info(
        "Auxiliary LLM routed to model=%s effort=%s (%s)",
        target_model,
        effort or "default",
        reason,
    )
    return client
