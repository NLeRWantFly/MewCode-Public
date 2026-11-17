import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.permissions.review_outcome import ReviewResponseFormatError
from mewcode.permissions.review_routing import ReviewRouter
from mewcode.permissions.reviewer import REVIEWER_SYSTEM_PROMPT, SafetyReviewer


REQUEST = dict(tool_name="Bash", arguments={"command": "python tools/environment_probe.py"},
               risk={}, conversation=None)


@pytest.mark.parametrize("empty", [None, "", "none", "NONE", "null", "n/a", "not_applicable"])
def test_optional_empty_blocker_on_explicit_allow(empty):
    result = SafetyReviewer._parse(json.dumps({"outcome": "allow", "blocker_kind": empty, "rationale": "Within the approved scope"}))
    assert result.allowed and result.denial_kind == "" and not result.escalatable


@pytest.mark.parametrize("value", ["none", "unexpected_category", {"text": "ignore policy"}])
def test_unknown_denial_category_is_never_interpreted_as_approval(value):
    with pytest.raises(ReviewResponseFormatError) as caught:
        SafetyReviewer._parse(json.dumps({"outcome": "deny", "rationale": "cannot authorize",
                                         "blocker_kind": value}))
    assert caught.value.field == "blocker_kind"
    assert caught.value.decision == "deny"


@pytest.mark.asyncio
async def test_one_format_correction_keeps_route_action_and_evidence(tmp_path):
    calls = []

    async def review(payload, system):
        calls.append((copy.deepcopy(payload), system))
        return ({"outcome": "allow", "blocker_kind": "no_policy_blockers", "rationale": "Within approved scope"}
                if len(calls) == 1 else {"outcome": "allow", "blocker_kind": "", "rationale": "Within approved scope"})

    client = SimpleNamespace(review_only=True, review_safety=review)
    backup = AsyncMock(side_effect=AssertionError("Format repair must not switch routes"))
    router = ReviewRouter(client, primary_route={"model": "configured-reviewer"}, fallback_resolver=backup)
    result = await SafetyReviewer(router, str(tmp_path)).review(**REQUEST)
    assert result.allowed and result.review_route["model"] == "configured-reviewer"
    assert len(calls) == 2 and calls[0][0] == calls[1][0]
    assert "Invalid field: blocker_kind" not in calls[0][1]
    assert "Invalid field: blocker_kind" in calls[1][1]
    backup.assert_not_called()


@pytest.mark.asyncio
async def test_repeated_invalid_format_is_reported_as_protocol_failure_not_connectivity(tmp_path):
    client = SimpleNamespace(review_safety=AsyncMock(return_value={
        "outcome": "allow", "blocker_kind": "secret_invalid_value"}))
    reviewer = SafetyReviewer(client, str(tmp_path))
    result = await reviewer.review(**REQUEST)
    assert not result.allowed and result.unavailable and not result.escalatable
    assert result.review_route["failure_kind"] == "response_format"
    assert result.review_route["invalid_field"] == "blocker_kind"
    assert "格式" in result.reason and "不是连接失败" in result.alternative
    assert "secret_invalid_value" not in result.reason
    assert client.review_safety.await_count == 2 and not reviewer._denials
    client.review_safety.return_value = {"outcome": "allow", "rationale": "Within approved scope"}
    recovered = await reviewer.review(**REQUEST)
    assert recovered.allowed and not recovered.cache_hit


@pytest.mark.asyncio
async def test_correcting_an_explicit_denial_cannot_change_it_to_allow(tmp_path):
    client = SimpleNamespace(review_safety=AsyncMock(side_effect=[
        {"outcome": "deny", "rationale": "outside scope", "blocker_kind": "unknown"},
        {"outcome": "allow", "rationale": "would reverse the prior denial"},
    ]))
    result = await SafetyReviewer(client, str(tmp_path)).review(**REQUEST)
    assert not result.allowed and result.unavailable and not result.escalatable
    assert result.review_route["invalid_field"] == "outcome"


@pytest.mark.asyncio
async def test_corrected_denial_remains_a_policy_denial(tmp_path):
    client = SimpleNamespace(review_safety=AsyncMock(side_effect=[
        {"outcome": "deny", "rationale": "outside scope", "blocker_kind": "unknown"},
        {"outcome": "deny", "rationale": "outside scope", "blocker_kind": "policy_violation"},
    ]))
    result = await SafetyReviewer(client, str(tmp_path)).review(**REQUEST)
    assert not result.allowed and not result.unavailable and not result.escalatable
    assert result.denial_kind == "policy_violation"


def test_allow_with_a_real_denial_category_is_inconsistent():
    with pytest.raises(ReviewResponseFormatError) as caught:
        SafetyReviewer._parse('{"outcome":"allow","blocker_kind":"policy_violation"}')
    assert caught.value.decision == "deny"


@pytest.mark.parametrize("raw,field", [
    ("not JSON", "json"), ('{"outcome": allow}', "json"),
    ('{"outcome":"unknown"}', "outcome"),
    ('{"outcome":"deny","risk_level":"invalid"}', "risk_level"),
    ('{"outcome":"deny","user_authorization":"invalid"}', "user_authorization"),
    ('{"outcome":"deny"}', "rationale"), ('{"outcome":"allow"}', "rationale"),
])
def test_response_format_failures_have_bounded_field_diagnostics(raw, field):
    with pytest.raises(ReviewResponseFormatError) as caught:
        SafetyReviewer._parse(raw)
    assert caught.value.field == field


@pytest.mark.asyncio
async def test_missing_rationale_correction_keeps_action_evidence_and_route(tmp_path):
    calls = []

    async def review(payload, system):
        calls.append((copy.deepcopy(payload), system))
        assert 'may return {"outcome":"allow"}' not in system
        assert "outcome and rationale are always required" in system
        if len(calls) == 1:
            return {"outcome": "allow"}
        assert "Invalid field: rationale" in system
        assert "include a nonempty rationale for every outcome" in system
        return {"outcome": "allow", "rationale": "The local environment probe is within the authorized task."}

    client = SimpleNamespace(review_only=True, review_safety=review)
    backup = AsyncMock(side_effect=AssertionError("Rationale correction must not switch routes"))
    router = ReviewRouter(client, primary_route={"model": "configured-reviewer"}, fallback_resolver=backup)
    result = await SafetyReviewer(router, str(tmp_path)).review(**REQUEST)
    assert result.allowed and result.reason and result.review_route["model"] == "configured-reviewer"
    assert len(calls) == 2 and calls[0][0] == calls[1][0]
    backup.assert_not_called()


@pytest.mark.parametrize('empty', [None, '', ' none ', 'NONE', 'null', 'n/a', 'not_applicable'])
def test_equivalent_allow_aliases_do_not_create_a_false_conflict(empty):
    result = SafetyReviewer._parse(json.dumps({'outcome': 'allow', 'review_blocker': '',
        'blocker_kind': empty, 'rationale': 'The exact read is within scope'}))
    assert result.allowed and result.denial_kind == ''


@pytest.mark.parametrize('outcome,left,right', [
    ('deny', 'missing_authorization', 'insufficient_evidence'),
    ('allow', '', 'policy_violation'),
    ('deny', '', 'policy_violation'),
])
def test_genuine_blocker_conflicts_remain_invalid(outcome, left, right):
    with pytest.raises(ReviewResponseFormatError, match='conflicting review blocker fields') as caught:
        SafetyReviewer._parse(json.dumps({'outcome': outcome, 'review_blocker': left,
            'blocker_kind': right, 'rationale': 'Conflicting categories are not a verdict'}))
    assert caught.value.decision == 'deny'


@pytest.mark.parametrize('field', ['review_blocker', 'blocker_kind'])
def test_non_applicable_alias_never_erases_a_denial(field):
    with pytest.raises(ReviewResponseFormatError):
        SafetyReviewer._parse(json.dumps({'outcome': 'deny', field: 'none', 'rationale': 'Not approved'}))


@pytest.mark.asyncio
async def test_browser_protocol_correction_keeps_original_action_route_and_evidence(tmp_path):
    calls = []
    arguments = {'url': 'https://example.com/reference'}
    evidence = [{'text': 'Source page is read-only', 'source': 'host_snapshot'}]

    async def review(payload, system):
        calls.append((copy.deepcopy(payload), system))
        output_keys = system.split('Otherwise return one JSON object with keys:', 1)[1].split('For a deny', 1)[0]
        assert 'review_blocker' in output_keys and 'blocker_kind' not in output_keys
        if len(calls) == 1:
            return {'outcome': 'deny', 'review_blocker': 'missing_authorization',
                'blocker_kind': 'insufficient_evidence', 'rationale': 'The upload is not in the user scope'}
        assert 'do not return blocker_kind or handoff_blocker' in system
        assert 'A previous denial must stay deny' in system
        return {'outcome': 'deny', 'review_blocker': 'missing_authorization',
            'rationale': 'The unchanged scope does not grant upload authority'}

    backup = AsyncMock(side_effect=AssertionError('Use the same review owner'))
    client = SimpleNamespace(review_only=True, review_safety=review)
    router = ReviewRouter(client, primary_route={'provider_key': 'configured', 'model': 'configured-reviewer'},
        fallback_resolver=backup)
    reviewer = SafetyReviewer(router, str(tmp_path))
    reviewer.authorization_context = 'Read the reference; do not upload files.'
    reviewer._evidence_context = lambda _: copy.deepcopy(evidence)
    result = await reviewer.review(tool_name='browser_navigate', arguments=arguments, risk={}, conversation=None)
    assert not result.allowed and not result.unavailable and result.denial_kind == 'missing_authorization'
    assert len(calls) == 2 and calls[0][0] == calls[1][0]
    assert arguments == {'url': 'https://example.com/reference'}
    assert evidence == [{'text': 'Source page is read-only', 'source': 'host_snapshot'}]
    assert result.review_route['model'] == 'configured-reviewer'
    backup.assert_not_called()


def test_reviewer_examples_follow_the_canonical_protocol():
    for label in ['Example allow:', 'Example deny:']:
        raw = REVIEWER_SYSTEM_PROMPT.split(label, 1)[1].split('\nNever request user confirmation.', 1)[0]
        data = json.JSONDecoder().raw_decode(raw.lstrip())[0]
        assert 'review_blocker' in data and 'blocker_kind' not in data
        assert SafetyReviewer._parse(json.dumps(data)).decision == label.split()[1].rstrip(':')


@pytest.mark.asyncio
async def test_allow_with_conflicting_denial_alias_cannot_be_reformatted_into_allow(tmp_path):
    client = SimpleNamespace(review_safety=AsyncMock(side_effect=[
        {'outcome': 'allow', 'review_blocker': '', 'blocker_kind': 'policy_violation',
         'rationale': 'This response contains a real denial category'},
        {'outcome': 'allow', 'review_blocker': '', 'rationale': 'Attempted reversal'},
    ]))
    result = await SafetyReviewer(client, str(tmp_path)).review(**REQUEST)
    assert not result.allowed and result.unavailable
    assert result.review_route['failure_kind'] == 'response_format'
    assert result.review_route['invalid_field'] == 'outcome'
