from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from frontend.server import _human_oncall_response_result


def _questions() -> list[dict[str, object]]:
    return [{
        "name": "choice",
        "options": [{"value": "continue"}, {"value": "pause"}],
        "allowOther": False,
        "required": True,
    }]


def _runtime(future: asyncio.Future[dict[str, str] | None]) -> SimpleNamespace:
    return SimpleNamespace(
        human_oncall_request=SimpleNamespace(
            request_id="request-1",
            questions=_questions(),
            future=future,
        )
    )


@pytest.mark.asyncio
async def test_human_oncall_response_ack_accepts_matching_choice() -> None:
    future: asyncio.Future[dict[str, str] | None] = asyncio.get_running_loop().create_future()
    result = _human_oncall_response_result(
        _runtime(future),
        {
            "request_id": "request-1",
            "conversation_id": "conversation-1",
            "answers": {"choice": "continue"},
        },
    )

    assert result == {
        "type": "human_oncall_response_result",
        "request_id": "request-1",
        "conversation_id": "conversation-1",
        "ok": True,
        "cancelled": False,
    }
    assert future.result() == {"choice": "continue"}


@pytest.mark.asyncio
async def test_human_oncall_response_ack_keeps_invalid_or_stale_request_live() -> None:
    future: asyncio.Future[dict[str, str] | None] = asyncio.get_running_loop().create_future()
    runtime = _runtime(future)
    invalid = _human_oncall_response_result(
        runtime,
        {
            "request_id": "request-1",
            "conversation_id": "conversation-1",
            "answers": {"choice": "other"},
        },
    )
    stale = _human_oncall_response_result(
        runtime,
        {
            "request_id": "old-request",
            "conversation_id": "conversation-1",
            "answers": {"choice": "continue"},
        },
    )

    assert invalid["ok"] is False and invalid["error_type"] == "invalid_answer"
    assert stale["ok"] is False and stale["error_type"] == "stale_request"
    assert not future.done()


@pytest.mark.asyncio
async def test_human_oncall_response_ack_explains_missing_live_future_without_resume() -> None:
    result = _human_oncall_response_result(
        None,
        {
            "request_id": "request-1",
            "conversation_id": "conversation-1",
            "answers": {"choice": "continue"},
        },
    )

    assert result["type"] == "human_oncall_response_result"
    assert result["ok"] is False
    assert result["error_type"] == "no_live_request"
    assert "恢复任务后再响应" in result["error"]
    assert "不会自动恢复" in result["error"]


@pytest.mark.asyncio
async def test_human_oncall_response_ack_accepts_active_cancel_as_null() -> None:
    future: asyncio.Future[dict[str, str] | None] = asyncio.get_running_loop().create_future()
    result = _human_oncall_response_result(
        _runtime(future),
        {
            "request_id": "request-1",
            "conversation_id": "conversation-1",
            "answers": {"choice": "continue"},
            "cancelled": True,
        },
    )

    assert result["ok"] is True and result["cancelled"] is True
    assert future.result() is None
