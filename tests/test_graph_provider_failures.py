import json
from types import SimpleNamespace

import httpx
import pytest

from frontend import orch_drafts, server
from mewcode.client import LLMError
from mewcode.orchestration.generation_failure import generation_failure
from tests.test_workflow_drafts import api_env as api_env
from tests.test_graph_generation import poll


@pytest.mark.parametrize("status,category,retryable", [(400, "request", False), (401, "authentication", False),
    (403, "authentication", False), (404, "route", False), (413, "capacity", False),
    (422, "request", False), (429, "rate_limit", True), (503, "upstream", True)])
def test_provider_error_keeps_status_and_redacts_keys(status, category, retryable):
    context = {"model": "selected-model", "requested_effort": "max", "provider": SimpleNamespace(
        api_key="opaque-configured-key", default_headers={"Authorization": "opaque-header-value"})}
    diagnostic = generation_failure(LLMError('invalid request: opaque-configured-key opaque-header-value Bearer SECRET', status_code=status), context)
    assert diagnostic["http_status"] == status and diagnostic["category"] == category
    assert diagnostic["retryable"] is retryable
    assert "selected-model" == diagnostic["route"]["model"]
    assert "opaque-configured-key" not in json.dumps(diagnostic) and "opaque-header-value" not in json.dumps(diagnostic)
    assert "Bearer SECRET" not in json.dumps(diagnostic)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_permanent_provider_rejection_stops_first_attempt_without_fake_success(api_env, monkeypatch, status):
    _, plans, _, owner = api_env
    calls = []
    async def rejected(client, **kwargs):
        calls.append(client)
        raise LLMError("unsupported reasoning effort", status_code=status)
    monkeypatch.setattr(orch_drafts, "collect_model_text", rejected)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        assert (await client.post("/api/orchestration/graph-autobuild/update", json=owner)).status_code == 202
        state = await poll(client, owner)
    assert len(calls) == 1 and state["coverage"]["processed"] == 0
    assert state["candidate"]["status"] == "failed" and "尚无成功块" in state["candidate"]["error"]
    row = state["candidate"]["log"][-1]
    assert row["diagnostic"]["http_status"] == status and row["attempt"] == 1
    assert row["request_chars"] > 0 and row["model_route"]["model"]
    assert not state["view"]["nodes"] and plans.get(owner["project"]) is None


def test_unknown_exception_does_not_publish_unlabelled_secret():
    diagnostic = generation_failure(RuntimeError("PRIVATE_PROVIDER_KEY"))
    assert diagnostic["detail"] == "" and "PRIVATE_PROVIDER_KEY" not in diagnostic["summary"]
