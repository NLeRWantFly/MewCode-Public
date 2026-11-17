import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.permissions.review_outcome import ReviewResponseFormatError
from mewcode.permissions.review_routing import ReviewRouter
from mewcode.permissions.reviewer import SafetyReviewer


def test_handoff_category_is_not_a_review_category_and_allow_requires_reason():
    assert SafetyReviewer._parse(json.dumps({'outcome': 'allow', 'review_blocker': '',
        'handoff_blocker': 'capability_missing', 'rationale': 'This action is covered'})).allowed
    with pytest.raises(ReviewResponseFormatError, match='missing reason'):
        SafetyReviewer._parse('{"outcome":"allow","review_blocker":""}')


@pytest.mark.asyncio
async def test_protocol_retries_twice_then_uses_independent_configured_provider(tmp_path):
    primary = SimpleNamespace(review_safety=AsyncMock(return_value={'outcome': 'allow',
        'review_blocker': 'capability_missing', 'rationale': 'legacy copied handoff'}))
    backup = SimpleNamespace(review_safety=AsyncMock(return_value={'outcome': 'allow',
        'review_blocker': '', 'rationale': 'Exact operation is within current authorization'}))
    router = ReviewRouter(primary, primary_route={'provider_key': 'one'},
        fallback_resolver=lambda: (backup, {'provider_key': 'two'}))
    result = await SafetyReviewer(router, str(tmp_path)).review(tool_name='Bash',
        arguments={'command': 'fixture-read'}, risk={}, conversation=None)
    assert result.allowed
    assert primary.review_safety.await_count == 2 and backup.review_safety.await_count == 1
    assert result.review_route['provider_key'] == 'two'
    assert len(result.review_route['protocol_attempts']) == 2
