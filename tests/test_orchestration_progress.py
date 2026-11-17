import asyncio

import pytest

from frontend import server
from mewcode.orchestration.spec import OrchestrationError
from mewcode.orchestration.extract import OrchestrationStreamTimeout


def test_orchestration_operation_id_validation() -> None:
    assert server._orchestration_operation_id({"operation_id": "orch-valid_123"}) == "orch-valid_123"
    assert server._orchestration_operation_id({}) == ""
    with pytest.raises(OrchestrationError, match="操作 id"):
        server._orchestration_operation_id({"operation_id": "bad id / path"})


@pytest.mark.asyncio
async def test_orchestration_model_call_has_no_total_wall_clock_timeout() -> None:
    operation_id = "orch-no-wall-clock-timeout"

    async def active_call() -> str:
        # Longer than the old test deadline. The stream-level inactivity
        # watchdog, not total duration, owns timeout decisions.
        await asyncio.sleep(0.03)
        return "done"

    result = await asyncio.wait_for(
        server._await_orchestration_model_call(operation_id, active_call()),
        timeout=1,
    )

    assert result == "done"
    assert operation_id not in server._ORCHESTRATION_ACTIVE_REQUESTS
    progress = server._ORCHESTRATION_OPERATION_PROGRESS[operation_id]
    assert progress["status"] == "running"
    assert progress["stage"] == "finalizing"
    assert progress["timeout_mode"] == "stage_inactivity"
    assert progress["reasoning_idle_timeout_seconds"] == 900
    assert progress["soft_idle_warning_seconds"] == 60


@pytest.mark.asyncio
async def test_orchestration_model_call_is_registered_and_cancellable() -> None:
    operation_id = "orch-cancel-test"
    started = asyncio.Event()

    async def pending_call() -> None:
        started.set()
        await asyncio.Future()

    request_task = asyncio.create_task(
        server._await_orchestration_model_call(operation_id, pending_call())
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    assert server._ORCHESTRATION_ACTIVE_REQUESTS[operation_id] is request_task

    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task

    assert operation_id not in server._ORCHESTRATION_ACTIVE_REQUESTS
    assert server._ORCHESTRATION_OPERATION_PROGRESS[operation_id]["status"] == "cancelled"


def test_orchestration_progress_reporter_tracks_real_stream_metrics() -> None:
    operation_id = "orch-stream-progress-test"
    server._start_orchestration_progress(operation_id, kind="graph")
    reporter = server._orchestration_progress_reporter(operation_id)
    assert reporter is not None

    reporter({
        "stage": "streaming",
        "detail": "模型正在流式输出阶段图 JSON。",
        "thinking_chars": 128,
        "thinking_summary_chars": 96,
        "thinking_preview": "正在规划节点依赖",
        "thinking_truncated": False,
        "output_chars": 512,
        "output_chunks": 4,
        "provider_event": True,
    })

    progress = server._ORCHESTRATION_OPERATION_PROGRESS[operation_id]
    assert progress["status"] == "running"
    assert progress["stage"] == "streaming"
    assert progress["thinking_chars"] == 128
    assert progress["thinking_summary_chars"] == 96
    assert progress["thinking_preview"] == "正在规划节点依赖"
    assert progress["thinking_truncated"] is False
    assert progress["output_chars"] == 512
    assert progress["output_chunks"] == 4
    assert progress["last_provider_event_at_ms"] > 0
    assert progress["last_activity_at_ms"] >= progress["last_provider_event_at_ms"]
    assert [event["stage"] for event in progress["events"]] == [
        "queued", "streaming"
    ]
    assert server._orchestration_timeout_diagnostic(operation_id).startswith(
        "模型已流式返回 512 个正文字符"
    )


def test_grok_xhigh_planner_uses_visible_latency_guard(monkeypatch) -> None:
    provider = server.ProviderConfig(
        name="desktop:example_provider_2",
        protocol="openai",
        base_url="https://api.x.ai/v1",
        model="grok-4.6",
        api_key="xai-test",
    )
    session = type("Session", (), {})()
    session.slot = server.ProviderSlot(
        provider=provider,
        source="desktop",
        registry_key="desktop:grok",
        effort_levels=["low", "medium", "high", "xhigh"],
        default_effort="high",
    )
    session.client = type("Client", (), {"effort": "xhigh"})()
    session.effort = "xhigh"
    session.effort_levels = lambda: ["low", "medium", "high", "xhigh"]
    session.usage_service = None
    session.work_dir = "C:/workspace"
    monkeypatch.setattr(server, "SESSION", session)

    context = server._orchestration_attempt_context()

    assert context["requested_effort"] == "xhigh"
    assert context["attempts"] == ["high", "medium"]
    assert context["policy"] == "latency_guard"


@pytest.mark.asyncio
async def test_orchestration_retries_once_with_lower_effort(monkeypatch) -> None:
    operation_id = "orch-effort-recovery"
    server._start_orchestration_progress(operation_id, kind="graph")
    context = {
        "provider_key": "desktop:grok",
        "provider_name": "desktop:example_provider_2",
        "model": "grok-4.6",
        "requested_effort": "xhigh",
        "attempts": ["high", "medium"],
        "policy": "latency_guard",
    }
    monkeypatch.setattr(
        server,
        "_new_orchestration_client",
        lambda _context, effort: effort,
    )
    attempts = []

    async def invoke(client, _reporter):
        attempts.append(client)
        if client == "high":
            raise OrchestrationStreamTimeout(
                phase="first_event", timeout_seconds=0.01
            )
        return {"ok": True}

    result = await server._run_orchestration_with_recovery(
        operation_id, context, invoke
    )

    assert result == {"ok": True}
    assert attempts == ["high", "medium"]
    progress = server._ORCHESTRATION_OPERATION_PROGRESS[operation_id]
    assert progress["attempt"] == 2
    assert progress["effective_effort"] == "medium"


@pytest.mark.asyncio
async def test_orchestration_does_not_duplicate_a_silent_reasoning_request(
    monkeypatch,
) -> None:
    operation_id = "orch-no-duplicate-reasoning"
    server._start_orchestration_progress(operation_id, kind="graph")
    context = {
        "provider_key": "desktop:grok",
        "provider_name": "desktop:example_provider_2",
        "model": "grok-4.6",
        "requested_effort": "xhigh",
        "attempts": ["high", "medium"],
        "policy": "latency_guard",
    }
    monkeypatch.setattr(
        server,
        "_new_orchestration_client",
        lambda _context, effort: effort,
    )
    attempts = []

    async def invoke(client, _reporter):
        attempts.append(client)
        raise OrchestrationStreamTimeout(
            phase="reasoning_idle", timeout_seconds=360
        )

    with pytest.raises(OrchestrationStreamTimeout):
        await server._run_orchestration_with_recovery(
            operation_id, context, invoke
        )

    assert attempts == ["high"]
    progress = server._ORCHESTRATION_OPERATION_PROGRESS[operation_id]
    assert progress["stalled_phase"] == "reasoning_idle"
    assert "不自动提交第二份请求" in progress["detail"]


@pytest.mark.asyncio
async def test_orchestration_retries_empty_structured_response(monkeypatch) -> None:
    operation_id = "orch-empty-recovery"
    server._start_orchestration_progress(operation_id, kind="graph")
    context = {
        "provider_key": "desktop:grok",
        "provider_name": "desktop:example_provider_2",
        "model": "grok-4.6",
        "requested_effort": "xhigh",
        "attempts": ["high", "medium"],
        "policy": "latency_guard",
    }
    monkeypatch.setattr(
        server,
        "_new_orchestration_client",
        lambda _context, effort: effort,
    )
    attempts = []

    async def invoke(client, _reporter):
        attempts.append(client)
        if client == "high":
            raise OrchestrationError("当前模型未返回阶段图内容，请重试或换模型后再抽")
        return {"ok": True}

    result = await server._run_orchestration_with_recovery(
        operation_id, context, invoke
    )

    assert result == {"ok": True}
    assert attempts == ["high", "medium"]
    assert server._ORCHESTRATION_OPERATION_PROGRESS[operation_id]["attempt"] == 2


@pytest.mark.asyncio
async def test_background_orchestration_keeps_result_after_http_return() -> None:
    operation_id = "orch-background-result"
    server._ORCHESTRATION_OPERATION_RESULTS.pop(operation_id, None)
    server._ORCHESTRATION_OPERATION_FAILURES.pop(operation_id, None)
    server._start_orchestration_progress(operation_id, kind="graph")

    async def execute():
        await asyncio.sleep(0)
        return {"spec": {"goal": "done"}}

    await server._run_orchestration_background_operation(
        operation_id,
        kind="graph",
        execute=execute,
    )

    assert server._ORCHESTRATION_OPERATION_RESULTS[operation_id] == {
        "spec": {"goal": "done"}
    }
    progress = server._ORCHESTRATION_OPERATION_PROGRESS[operation_id]
    assert progress["status"] == "completed"
    assert progress["result_ready"] is True
    assert operation_id not in server._ORCHESTRATION_ACTIVE_REQUESTS


@pytest.mark.asyncio
async def test_background_orchestration_persists_failure_for_polling() -> None:
    operation_id = "orch-background-failure"
    server._ORCHESTRATION_OPERATION_RESULTS.pop(operation_id, None)
    server._ORCHESTRATION_OPERATION_FAILURES.pop(operation_id, None)
    server._start_orchestration_progress(operation_id, kind="graph")

    async def execute():
        raise OrchestrationError("结构无效")

    await server._run_orchestration_background_operation(
        operation_id,
        kind="graph",
        execute=execute,
    )

    assert server._ORCHESTRATION_OPERATION_FAILURES[operation_id]["error"] == "结构无效"
    assert server._ORCHESTRATION_OPERATION_PROGRESS[operation_id]["status"] == "failed"


@pytest.mark.asyncio
async def test_background_stream_stall_is_reported_as_timeout() -> None:
    operation_id = "orch-background-stream-timeout"
    server._ORCHESTRATION_OPERATION_RESULTS.pop(operation_id, None)
    server._ORCHESTRATION_OPERATION_FAILURES.pop(operation_id, None)
    server._start_orchestration_progress(operation_id, kind="graph")

    async def execute():
        raise OrchestrationStreamTimeout(
            phase="first_event", timeout_seconds=0.01
        )

    await server._run_orchestration_background_operation(
        operation_id,
        kind="graph",
        execute=execute,
    )

    failure = server._ORCHESTRATION_OPERATION_FAILURES[operation_id]
    assert failure["timeout"] is True
    assert failure["stalled_phase"] == "first_event"
    assert server._ORCHESTRATION_OPERATION_PROGRESS[operation_id]["status"] == "timeout"


@pytest.mark.asyncio
async def test_background_upstream_idle_has_actionable_provider_diagnostic() -> None:
    operation_id = "orch-background-upstream-idle"
    server._ORCHESTRATION_OPERATION_RESULTS.pop(operation_id, None)
    server._ORCHESTRATION_OPERATION_FAILURES.pop(operation_id, None)
    server._start_orchestration_progress(operation_id, kind="graph")

    async def execute():
        raise server.LLMError(
            "Provider 响应未完成：upstream_stream_idle_timeout: "
            "上游流式响应长时间无数据"
        )

    await server._run_orchestration_background_operation(
        operation_id,
        kind="graph",
        execute=execute,
    )

    failure = server._ORCHESTRATION_OPERATION_FAILURES[operation_id]
    assert failure["timeout"] is True
    assert failure["provider_error_code"] == "upstream_stream_idle_timeout"
    assert "正文、推理摘要或工具进展" in failure["error"]
    progress = server._ORCHESTRATION_OPERATION_PROGRESS[operation_id]
    assert progress["status"] == "timeout"
    assert progress["provider_error_code"] == "upstream_stream_idle_timeout"
