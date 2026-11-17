from unittest.mock import AsyncMock, MagicMock

import pytest

from mewcode.config import ProviderConfig
from mewcode.permissions.reviewer import SafetyReviewer
from mewcode.typesafe_client import TypeSafeClient


@pytest.mark.asyncio
async def test_oversized_review_stops_before_network_without_retry(tmp_path):
    client = TypeSafeClient(ProviderConfig(
        name="Jev", protocol="typesafe", base_url="https://api.typesafe.ai/v1",
        model="jev-latest", api_key="test",
    ))
    client._request = AsyncMock()
    client.evaluate = AsyncMock(wraps=client.evaluate)
    client.preflight_safety_review = MagicMock(wraps=client.preflight_safety_review)
    reviewer = SafetyReviewer(client, str(tmp_path))
    command = "证据" * 10_000
    result = await reviewer.review(
        tool_name="Bash", arguments={"command": command}, risk={}, conversation=None,
    )
    assert result.unavailable and not result.allowed and not result.inconclusive
    assert result.review_route["status"] == "context_too_large"
    assert result.review_route["failure_kind"] == "text_input"
    assert "先修复图像输入" not in result.alternative
    sizes = result.review_route["context_sizes"]
    assert sizes["field_bytes"]["planned_action"] > 60_000
    assert "请求未发送" in result.reason
    assert "连续两次" not in result.reason
    client.evaluate.assert_not_awaited()
    client._request.assert_not_awaited()
    client.preflight_safety_review.assert_called_once()
    arguments = client.preflight_safety_review.call_args.args[0]["planned_action"]["arguments"]
    assert arguments["command"] == command
