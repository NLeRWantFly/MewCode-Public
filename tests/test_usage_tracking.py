from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import threading

import pytest

from mewcode.client import LLMClient
from mewcode.cache_slots import resolve_cache_usage_provider
from mewcode.config import ProviderConfig, UsageConfig
from mewcode.tools.base import StreamEnd
from mewcode.usage.ledger import UsageLedger
from mewcode.usage.models import (
    AttributionStatus,
    EvidenceLevel,
    RateLimitSnapshot,
    RequestUsage,
    TokenUsage,
)
from mewcode.usage.service import usage_trace_scope


UTC = timezone.utc


class DummyClient(LLMClient):
    async def stream(
        self,
        conversation,
        system: str = "",
        tools: list[dict] | None = None,
    ):
        # Abstract async-generator implementation; normalization tests call
        # _record_stream_end directly and never consume this method.
        if False:
            yield StreamEnd(stop_reason="stop")


@pytest.mark.asyncio
async def test_slow_usage_commit_keeps_other_nodes_responsive_and_preserves_scope():
    client = DummyClient()
    client.configure_usage_tracking(ProviderConfig(name='fixture', protocol='openai',
        model='gpt-6.1-sol', base_url='https://api.openai.com/v1'), endpoint='/v1/responses')
    started, release = threading.Event(), threading.Event()
    records = []
    def save(record):
        started.set()
        assert release.wait(5), 'test must release the simulated durable writer'
        records.append(record)
    client.set_usage_recorder(save)
    event = StreamEnd(stop_reason='end_turn', response_id='saved-response', output_tokens=1)
    with usage_trace_scope(session_id='parallel-session', agent_id='node-1', trace_id='trace-1'):
        task = asyncio.create_task(client._record_stream_end_async(event))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            await asyncio.sleep(0)
            assert not task.done() and records == []
        finally:
            release.set()
        assert await asyncio.wait_for(task, 2) is event
    assert len(records) == 1 and records[0].session_id == 'parallel-session'
    assert records[0].agent_id == 'node-1' and records[0].trace_id == 'trace-1'


def test_ledger_aggregates_cache_reasoning_and_evidence(tmp_path) -> None:
    ledger = UsageLedger(tmp_path)
    record = RequestUsage(
        provider_name="openai",
        configured_model="gpt-5.6",
        response_model="gpt-5.6-2026-07-01",
        tokens=TokenUsage(
            input_tokens=600,
            cached_input_tokens=300,
            cache_write_input_tokens=100,
            output_tokens=200,
            reasoning_tokens=80,
        ),
        cache_usage_available=True,
        rate_limits=RateLimitSnapshot(
            request_limit=100,
            requests_remaining=99,
            token_limit=100_000,
            tokens_remaining=98_800,
        ),
        evidence=EvidenceLevel.OFFICIAL_RESPONSE,
        attribution=AttributionStatus.REQUEST_VERIFIED,
    )

    ledger.append(record)
    ledger.append(record)  # event_id makes retries/idempotent writes safe

    summary = ledger.summarize(provider_name="openai")
    assert summary.request_count == 1
    assert summary.tokens.input_total == 1_000
    assert summary.tokens.output_tokens == 200
    assert summary.tokens.reasoning_tokens == 80
    assert summary.by_model["gpt-5.6-2026-07-01"].cached_input_tokens == 300
    assert summary.evidence_counts == {"official_response": 1}
    assert summary.latest_rate_limits is not None
    assert summary.latest_rate_limits.tokens_remaining == 98_800
    recent = ledger.recent(provider_name="openai")
    assert recent[0]["prompt_tokens"] == 1_000
    assert recent[0]["cache_hit_rate"] == 0.3


def test_client_normalizes_direct_request_and_attaches_agent_scope() -> None:
    client = DummyClient()
    client.cache_usage_provider = resolve_cache_usage_provider("generic", "openai")
    provider = ProviderConfig(
        name="openai",
        protocol="openai",
        base_url="https://api.openai.com/v1",
        model="gpt-5.6",
        api_key="sk-test-do-not-store",
        default_headers={"OpenAI-Project": "proj_mewcode"},
    )
    client.configure_usage_tracking(provider, endpoint="/v1/responses")
    records: list[RequestUsage] = []
    client.set_usage_recorder(records.append)

    with usage_trace_scope(session_id="session-1", agent_id="agent-1", trace_id="trace-1"):
        event = StreamEnd(
            stop_reason="end_turn",
            input_tokens=10,
            cache_read=3,
            cache_creation=2,
            output_tokens=5,
            reasoning_tokens=1,
            response_id="resp_123",
            model="gpt-5.6-2026-07-01",
            service_tier="default",
        )
        assert client._record_stream_end(
            event,
            {
                "x-request-id": "req_123",
                "openai-organization": "org_123",
                "x-ratelimit-limit-tokens": "1000",
                "x-ratelimit-remaining-tokens": "980",
            },
        ) is event

    assert len(records) == 1
    record = records[0]
    assert record.tokens.input_total == 15
    assert record.evidence is EvidenceLevel.OFFICIAL_RESPONSE
    assert record.attribution is AttributionStatus.REQUEST_VERIFIED
    assert record.organization_id == "org_123"
    assert record.project_id == "proj_mewcode"
    assert record.request_id == "req_123"
    assert record.upstream_request_id == "req_123"
    assert record.session_id == "session-1"
    assert record.agent_id == "agent-1"
    assert record.trace_id == "trace-1"
    assert record.credential_fingerprint
    assert record.metadata["cache_slot"] == "openai_prompt"
    assert record.metadata["cache_provider"] == "openai"
    assert "sk-test" not in record.credential_fingerprint


def test_opaque_relay_is_never_labeled_as_verified() -> None:
    client = DummyClient()
    provider = ProviderConfig(
        name="relay",
        protocol="openai-compat",
        base_url="https://relay.example/v1",
        model="gpt-5.6",
        api_key="relay-key",
        usage=UsageConfig(account_tracking=True, attribution_mode="managed-relay"),
    )
    client.configure_usage_tracking(provider, endpoint="/v1/chat/completions")
    records: list[RequestUsage] = []
    client.set_usage_recorder(records.append)

    client._record_stream_end(
        StreamEnd(
            stop_reason="stop",
            input_tokens=12,
            output_tokens=4,
            response_id="relay_response",
        )
    )

    assert records[0].evidence is EvidenceLevel.RELAY_REPORTED
    assert records[0].attribution is AttributionStatus.UNVERIFIABLE
    assert not records[0].upstream_request_id


def test_ledger_time_filter(tmp_path) -> None:
    ledger = UsageLedger(tmp_path)
    ledger.append(
        RequestUsage(
            occurred_at=datetime(2026, 8, 2, tzinfo=UTC),
            provider_name="openai",
            tokens=TokenUsage(input_tokens=7),
        )
    )
    summary = ledger.summarize(
        start_time=datetime(2026, 8, 1, tzinfo=UTC),
        end_time=datetime(2026, 9, 1, tzinfo=UTC),
    )
    assert summary.request_count == 1
    assert summary.tokens.input_tokens == 7
