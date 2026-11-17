from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from mewcode.config import ProviderConfig
from mewcode.model_registry import ProviderRegistry
from mewcode.provider_sources import ProviderSlot


@pytest.mark.asyncio
async def test_fast_catalog_does_not_call_network_or_overwrite_live_cache():
    def no_client(*args):
        raise AssertionError("Opening a picker contacted an upstream")

    # Seed a live cache only after the registry's desktop revision baseline is
    # synchronized; otherwise the first catalog call performs initial sync.
    desktop_source = ProviderRegistry(client_factory=no_client)
    registry = ProviderRegistry(
        client_factory=no_client,
        desktop_source=desktop_source,
    )
    registry.refresh_discovered([ProviderSlot(
        provider=ProviderConfig(name="configured", protocol="openai", base_url="https://relay.test/v1",
                                model="gpt-5.6-sol", api_key="test"),
        source="config", models=["gpt-5.6-sol", "codex-auto-review"],
    )])
    registry._catalog_desktop_revision = registry.desktop_revision()
    original_cache = {"models": [{"model_id": "old-live-model"}]}
    registry._catalog_cache = original_cache
    result = await registry.catalog(local_only=True)
    assert result["catalog_mode"] == "configured"
    assert [row["model_id"] for row in result["models"]] == ["gpt-5.6-sol"]
    assert result["models"][0]["available"]
    assert result["models"][0]["provider_key"]
    assert registry._catalog_cache is original_cache

    # A real desktop-profile revision change invalidates a project registry's
    # otherwise-live catalog cache on its next local-only read.
    changed_cache = {"models": [{"model_id": "stale-desktop-model"}]}
    registry._catalog_cache = changed_cache
    desktop_source.replace_desktop_profiles([{
        "provider_key": "desktop-relay",
        "name": "Desktop relay",
        "protocol": "openai",
        "base_url": "https://relay.test/v1",
        "default_model": "gpt-5.6-sol",
        "models": ["gpt-5.6-sol"],
        "api_key": "test",
    }])
    changed_revision = desktop_source.desktop_revision()
    assert changed_revision != registry._catalog_desktop_revision
    await registry.catalog(local_only=True)
    assert registry._catalog_cache is None
    assert registry._catalog_desktop_revision == changed_revision


@pytest.mark.asyncio
@pytest.mark.parametrize("query,local_only", [("fast=true", True), ("refresh=true&include_intelligence=false", False)])
async def test_picker_endpoint_never_waits_for_optional_ranking(query, local_only, monkeypatch):
    from frontend import server

    catalog = AsyncMock(return_value={"models": [{"model_id": "grok-4.6", "provider_key": "local"}]})
    intelligence = AsyncMock(side_effect=AssertionError("Picker waited for optional IQ feed"))
    monkeypatch.setattr(server.SESSION, "sync_cc_switch_mirror", lambda: None)
    monkeypatch.setattr(server, "MODEL_REGISTRY", SimpleNamespace(catalog=catalog))
    monkeypatch.setattr(server, "MODEL_INTELLIGENCE", SimpleNamespace(get=intelligence))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.get("/api/models/catalog?" + query)
    assert response.status_code == 200
    assert response.json()["models"][0]["provider_key"] == "local"
    assert catalog.call_args.kwargs == {"force": not local_only, "local_only": local_only}
    intelligence.assert_not_called()


@pytest.mark.parametrize("protocol", ["openai", "openai-compat", "anthropic"])
def test_request_error_identifies_actual_route_without_url_credentials(protocol):
    from mewcode.client import create_client
    from mewcode.errors import is_transient_error

    client = create_client(ProviderConfig(name="Grok local", protocol=protocol,
        base_url="https://user:password@relay.test/v1?api_key=secret", model="grok-4.6", api_key="test-key"))
    response = httpx.Response(502, headers={"x-request-id": "test-request"},
        request=httpx.Request("POST", "https://user:password@relay.test/v1/responses?token=secret"))
    error = client._api_status_error(SimpleNamespace(status_code=502, message="upstream_network_error", response=response))
    assert error.status_code == 502 and is_transient_error(error)
    assert "Provider=Grok local" in str(error) and "model=grok-4.6" in str(error)
    assert "URL=https://relay.test/v1/responses" in str(error) and "request_id=test-request" in str(error)
    assert "password" not in str(error) and "secret" not in str(error) and "test-key" not in str(error)


def test_resume_does_not_mix_recorded_404_into_new_provider_output():
    from mewcode.expert_web import _resumed_stage_text

    error_404 = 'API error (404): Model "grok-4.6" is not supported by the group'
    error_502 = 'API error (502): upstream_network_error'
    authored = "已核验交付物，保留现有证据。"
    stage = {"partial_text": "\n\n" + error_404 + authored + "\n\n" + error_502}
    events = [
        {"type": "expert_node_completed", "node_id": "done", "status": "failed", "text": error_404},
        {"type": "expert_node_completed", "node_id": "done", "status": "failed", "text": error_404 + authored + "\n\n" + error_502},
    ]
    before = stage.copy()
    assert _resumed_stage_text(stage, events, "done") == authored
    assert stage == before  # Stored old attempt/history is not rewritten.
    assert _resumed_stage_text(stage, events, "other") == stage["partial_text"].strip()
    assert _resumed_stage_text({**stage, "output_diagnostics_separated": True}, events, "done") == stage["partial_text"]


def test_resume_keeps_literal_error_example_without_a_matching_failed_event():
    from mewcode.expert_web import _resumed_stage_text
    authored = "文档示例：\n\nAPI error (502): transport error"
    assert _resumed_stage_text({"partial_text": authored}, [], "node") == authored
    events = [{"type": "expert_node_completed", "node_id": "node", "status": "failed", "text": authored}]
    assert _resumed_stage_text({"partial_text": authored, "messages": [{"role": "assistant", "content": authored}]},
                               events, "node") == authored


@pytest.mark.asyncio
async def test_request_failure_checkpoint_resumes_without_error_as_authored_text(tmp_path, monkeypatch):
    from mewcode import expert_web
    from mewcode.agent import StreamText
    from mewcode.client import LLMError
    from mewcode.conversation import ConversationManager
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.experts.flow import ExpertFlowSpec, ExpertFlowStore
    from mewcode.orchestration.plan_session import PlanSessionStore
    from tests.test_expert_flow import _profile

    project = tmp_path / "project"
    project.mkdir()
    runs = ExpertRunStore(tmp_path / "runs")
    flows = ExpertFlowStore(tmp_path / "flows.json")
    flows.put(project, ExpertFlowSpec.from_value({
        "flow_id": "error-resume", "label": "Resume", "finalizer_node_id": "done",
        "nodes": [{"node_id": "done", "role": "manager", "label": "交付",
                   "model": {"provider_key": "local-grok", "model_id": "grok-4.6"}}], "edges": [],
    }))
    fail = True
    histories = []

    class FakeAgent:
        client = SimpleNamespace(expose_reasoning=False)
        total_input_tokens = 1
        total_output_tokens = 1

        def set_review_authorization_context(self, _context):
            pass

        async def run(self, conversation):
            histories.append(list(conversation.history))
            text = "已核验日志" if fail else "交付验证完成"
            conversation.add_assistant_message(text)
            yield StreamText(text=text)
            if fail:
                raise LLMError("API error (502): upstream_network_error", status_code=502)

    monkeypatch.setattr(expert_web, "_build_role_agent", lambda *args: (
        FakeAgent(), None, SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())))
    durable = SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda *args: None,
                              append_expert_run=lambda *args, **kwargs: None)
    session = SimpleNamespace(work_dir=str(project), conversation_id="c", multi_agent=False,
        session=durable, conversation=ConversationManager(), expert_run_id="", expert_control_action="pause",
        context=SimpleNamespace(get=lambda key, default=None: default, slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_child_event_callback=None, status_payload=lambda: {}, tasks_payload=lambda: [])
    registry = SimpleNamespace(catalog=AsyncMock(return_value={"providers": []}), resolve=lambda model: None)
    kwargs = dict(session=session, registry_service=registry, profile_store=SimpleNamespace(get=lambda p: _profile()),
        flow_store=flows, plan_store=PlanSessionStore(tmp_path / "plans.json"), run_store=runs)
    ws = SimpleNamespace(send_json=AsyncMock())
    await expert_web.run_expert_chat(ws, "核验交付", confirmed_new_flow=flows.get(project), **kwargs)
    saved = runs.latest_resumable("c")
    assert saved is not None
    state = saved["node_states"]["done"]
    assert state["partial_text"] == "已核验日志"
    assert state["request_error"] == "API error (502): upstream_network_error"
    assert state["output_diagnostics_separated"]
    fail = False
    await expert_web.run_expert_chat(ws, "继续", resume_run_id=saved["run_id"], **kwargs)
    assert runs.load(saved['run_id'])['status'] == 'paused'
    assert len(histories) == 1
    from mewcode.experts.node_ledger import bump_run_epoch
    runs.mutate(saved['run_id'], lambda r: bump_run_epoch(r, 'done', reason='maintenance_applied',
        source_ref='user_confirmed:provider_recovered'))
    await expert_web.run_expert_chat(ws, '核验已恢复的模型服务', resume_run_id=saved['run_id'], **kwargs)
    finished = runs.load(saved["run_id"])
    assert finished["status"] == "completed"
    assert "API error" not in finished["node_states"]["done"]["text"]
    assert "已核验日志" in finished["node_states"]["done"]["text"]
    assert not finished["node_states"]["done"]["request_error"]
    assert any("已核验日志" in str(message.content) for message in histories[-1])


def test_model_picker_in_real_canvas_renderer(tmp_path):
    import json
    import os
    from pathlib import Path
    import subprocess
    from mewcode.orchestration import OrchestrationSpec, to_pipeline_graph

    root = Path(__file__).resolve().parents[1]
    electron = root / "client/node_modules/electron/dist/electron.exe"
    if not electron.exists():
        pytest.skip("Optional desktop renderer unavailable")
    spec = OrchestrationSpec.from_value({
        "spec_id": "picker", "label": "Model selection", "finalizer_node_id": "done",
        "nodes": [{"node_id": "done", "kind": "reduce", "label": "交付结果", "bind_later": True}], "edges": [],
    })
    fixture = tmp_path / "graph.json"
    fixture.write_text(json.dumps(to_pipeline_graph(spec).model_dump(mode="json", by_alias=True)), encoding="utf-8")
    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    result = subprocess.run([str(electron), str(root / "tests/orch_model_picker_renderer.cjs"), str(tmp_path), str(fixture)],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "picker-report.json").read_text(encoding="utf-8"))
    assert report["selectable"] == 5 and report["gpt61Sol"]
