from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from frontend import server
from mewcode.agents.task_manager import BackgroundTask
from mewcode.cache_slots import (
    CacheTotals,
    normalized_cache_usage,
    resolve_cache_usage_provider,
)
from mewcode.config import ProviderConfig
from mewcode.memory import SessionManager


def test_context_tiers_are_sorted_unique_and_match_the_ui_buttons() -> None:
    """The cap list and the runtime buttons must not drift apart.

    A tier the UI offers but the server rejects silently changes the window, so
    the two lists are asserted to agree.
    """
    tiers = server.CONTEXT_TIERS
    assert tiers == sorted(tiers)
    assert len(set(tiers)) == len(tiers)
    assert tiers[0] == 0  # auto: use the model's full window
    assert 200_000 in tiers  # preserve saved manual caps
    app_js = (Path(__file__).resolve().parent.parent / "frontend" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    for tier in tiers:
        assert f"{{ value: {tier}," in app_js, f"tier {tier} is missing from the runtime buttons"
    # Every tier is a real published window: nothing above the largest one.
    assert max(tiers) == 1_050_000  # GPT-5.6, also covers 1,048,576 models


@pytest.mark.parametrize("value", [None, "bad", "", True, 0.5, {}])
def test_invalid_context_tier_does_not_turn_into_auto(value: object) -> None:
    assert server._parse_context_tier(value) == -1
    assert server._parse_context_tier(0) == 0


def test_context_tier_auto_uses_full_model_window_and_manual_cap_is_preserved(
    monkeypatch, tmp_path: Path
) -> None:
    _patch_web_files(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    session = server.WebSession()
    assert session.context_tier == 0
    provider = ProviderConfig(
        name="OpenAI", protocol="openai", base_url="https://api.openai.com/v1",
        model="gpt-6-sol", api_key="test",
    )
    session.slot = SimpleNamespace(provider=provider, key="openai:test")
    session.agent = SimpleNamespace(context_window=0)

    session._apply_context_tier()
    assert session.agent.context_window == 1_050_000
    assert session.set_context_tier(1_000_000) is None
    assert session.agent.context_window == 1_000_000
    runtime = SimpleNamespace(context_tier=1_000_000, running=lambda: True)
    session.session = SimpleNamespace(session_id="active")
    session.conversation_runs["active"] = runtime
    assert session.set_context_tier(0) is None
    assert session.agent.context_window == 1_050_000
    assert runtime.context_tier == 0
    assert server._load_web_state()["context_tier"] == 0


def test_status_payloads_reconcile_stale_agent_window_and_preserve_manual_caps(
    tmp_path: Path,
) -> None:
    import time

    provider = ProviderConfig(
        name="OpenAI", protocol="openai", base_url="https://api.openai.com/v1",
        model="gpt-6-sol", api_key="test",
    )
    slot = SimpleNamespace(
        provider=provider, key="openai:test", effort_levels=[], default_effort=""
    )

    def agent_with_stale_window():
        return SimpleNamespace(
            context_window=105_000,
            total_input_tokens=0,
            total_output_tokens=0,
            total_cache_read_tokens=0,
            total_cache_creation_tokens=0,
            cache_usage_available=False,
            permission_mode=SimpleNamespace(value="default"),
            context_policy=None,
            context_channel="",
            cache_observations=[],
        )

    web = object.__new__(server.WebSession)
    web.slot = slot
    web.agent = agent_with_stale_window()
    web.context_tier = 0
    web.session = None
    web.session_start = time.monotonic()
    web.work_dir = str(tmp_path)
    web.scope = "project"
    web.multi_agent = False
    web.expert_mode = False
    web.orchestration_planning = False
    web.conversation_goals = {}
    web.efforts_payload = lambda: {"current": "auto"}

    web_status = web.status_payload()
    assert web_status["window"] == 1_050_000
    assert web.agent.context_window == 1_050_000

    runtime = object.__new__(server.ConversationRuntime)
    runtime.slot = slot
    runtime.agent = agent_with_stale_window()
    runtime.context_tier = 0
    runtime.client = None
    runtime.effort = ""
    runtime.owner = SimpleNamespace(
        session_start=time.monotonic(), orchestration_planning=False
    )
    runtime.session = SimpleNamespace(
        meta=SimpleNamespace(title="test", continuous_task={})
    )
    runtime.conversation_id = "conversation"
    runtime.work_dir = str(tmp_path)
    runtime.scope = "project"
    runtime.multi_agent = False
    runtime.expert_mode = False
    runtime.execution_kind = "chat"
    runtime.goal = ""

    runtime_status = runtime.status_payload()
    assert runtime_status["window"] == 1_050_000
    assert runtime.agent.context_window == 1_050_000

    web.context_tier = 1_000_000
    web.agent.context_window = 105_000
    runtime.context_tier = 1_000_000
    runtime.agent.context_window = 105_000
    assert web.status_payload()["window"] == 1_000_000
    assert web.agent.context_window == 1_000_000
    assert runtime.status_payload()["window"] == 1_000_000
    assert runtime.agent.context_window == 1_000_000

    # An explicit smaller provider window remains authoritative in Auto mode.
    provider.context_window = 90_000
    web.context_tier = 0
    web.agent.context_window = 105_000
    assert web.status_payload()["window"] == 90_000
    assert web.agent.context_window == 90_000


def _patch_web_files(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / "web_state.json")
    monkeypatch.setattr(server, "PROJECTS_FILE", tmp_path / "projects.json")
    monkeypatch.setattr(
        server, "BACKGROUND_TASKS_FILE", tmp_path / "background_tasks.json"
    )


def test_add_project_only_registers_an_existing_directory(
    monkeypatch, tmp_path: Path
) -> None:
    _patch_web_files(monkeypatch, tmp_path)
    existing = tmp_path / "existing-project"
    missing = tmp_path / "missing-project"
    existing.mkdir()

    added = asyncio.run(server.api_project_add(server.DirBody(dir=str(existing))))
    rejected = asyncio.run(server.api_project_add(server.DirBody(dir=str(missing))))

    assert added.status_code == 200
    assert server._load_projects() == [str(existing)]
    assert rejected.status_code == 404
    assert not missing.exists()


def test_remove_project_only_unregisters_it(monkeypatch, tmp_path: Path) -> None:
    active = tmp_path / "active"
    removable = tmp_path / "removable"
    active.mkdir()
    session_dir = removable / ".mewcode" / "sessions"
    session_dir.mkdir(parents=True)
    history = session_dir / "conversation.jsonl"
    history.write_text('{"role":"user","text":"keep me"}\n', encoding="utf-8")
    _patch_web_files(monkeypatch, tmp_path)
    server._register_project(str(active))
    server._register_project(str(removable))
    monkeypatch.setattr(server, "SESSION", SimpleNamespace(work_dir=str(active)))

    removed = asyncio.run(server.api_project_remove(server.DirBody(dir=str(removable))))

    assert removed.status_code == 200
    assert str(removable) not in server._load_projects()
    assert removable.is_dir()
    assert history.read_text(encoding="utf-8") == '{"role":"user","text":"keep me"}\n'

    protected = asyncio.run(server.api_project_remove(server.DirBody(dir=str(active))))
    assert protected.status_code == 400
    assert str(active) in server._load_projects()


def test_web_session_restores_saved_active_project(monkeypatch, tmp_path: Path) -> None:
    launch = tmp_path / "launch"
    project = tmp_path / "project"
    launch.mkdir()
    project.mkdir()
    _patch_web_files(monkeypatch, tmp_path)
    server.STATE_FILE.write_text(
        json.dumps({"active_project": str(project)}), encoding="utf-8"
    )
    monkeypatch.chdir(launch)

    session = server.WebSession()

    assert session.work_dir == str(project)
    assert session.project_sessions._sessions_dir.parent.parent == project
    assert session.usage_service.ledger.path == (
        project / ".mewcode" / "usage" / "ledger.sqlite3"
    )


def test_desktop_web_session_uses_unified_user_data_ledger(
    monkeypatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _patch_web_files(monkeypatch, tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setattr(server, "DESKTOP_TOKEN", "desktop-session")

    session = server.WebSession()

    assert session.usage_service.ledger.path == server.USAGE_RUNTIME.ledger.path


def test_project_conversation_state_records_its_directory(
    monkeypatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _patch_web_files(monkeypatch, tmp_path)
    monkeypatch.chdir(project)
    session = server.WebSession()

    created = session.new_conversation("project")
    state = json.loads(server.STATE_FILE.read_text(encoding="utf-8"))

    assert state["last_conversation"] == {
        "scope": "project",
        "id": created["id"],
        "dir": str(project),
    }
    session._close_session()


def test_new_conversation_leaves_expert_mode_behind(
    monkeypatch, tmp_path: Path
) -> None:
    """Confirming an orchestration must not trap every later chat in expert mode."""
    project = tmp_path / "project"
    project.mkdir()
    _patch_web_files(monkeypatch, tmp_path)
    monkeypatch.chdir(project)
    session = server.WebSession()
    previous = session.new_conversation("project")
    # Sticky expert mode left behind after 「确认开跑」.
    session.expert_mode = True
    session.orchestration_planning = True
    session.web_state["expert_mode"] = True
    session.web_state["orchestration_planning"] = True
    session.context_tier = 1_000_000
    session.web_state["context_tier"] = 1_000_000
    session._save_active_conversation_settings()

    created = session.new_conversation("project")
    state = json.loads(server.STATE_FILE.read_text(encoding="utf-8"))

    assert created["id"] != previous["id"]
    assert session.expert_mode is False
    assert session.orchestration_planning is False
    assert session.context_tier == 0
    assert state["expert_mode"] is False
    assert state["conversation_settings"][previous["id"]]["context_tier"] == 1_000_000
    assert state["conversation_settings"][created["id"]]["context_tier"] == 0
    assert state["conversation_settings"][created["id"]]["expert_mode"] is False


def test_restore_conversation_without_expert_flag_defaults_to_normal_chat(
    monkeypatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _patch_web_files(monkeypatch, tmp_path)
    monkeypatch.chdir(project)
    session = server.WebSession()
    session.expert_mode = True
    session.web_state["expert_mode"] = True
    session.conversation_settings["old_chat"] = {
        "provider_key": "",
        "effort": "",
        "context_tier": 272000,
        "permission_mode": "auto",
        "multi_agent": False,
        # Intentionally omit expert_mode — old rows should not inherit sticky True.
    }

    session._restore_conversation_settings("old_chat")

    assert session.expert_mode is False
    assert session.multi_agent is False
    assert session.conversation_settings["old_chat"]["multi_agent"] is False


def test_continue_is_only_reserved_when_expert_recovery_is_possible() -> None:
    """Resume belongs to current focus, never a sticky mode or old checkpoint."""
    flow = {"kind": "orchestration", "run_id": "paused-run"}
    assert not server._should_route_expert_resume("继续", focus={})
    assert not server._should_route_expert_resume("继续", focus={"kind": "chat"})
    assert server._should_route_expert_resume("继续", focus=flow)
    assert server._should_route_expert_resume("恢复编排", focus={"kind": "chat"})
    assert not server._should_route_expert_resume("继续安装技能", focus=flow)


def test_web_preferences_survive_restart(monkeypatch, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _patch_web_files(monkeypatch, tmp_path)
    monkeypatch.chdir(project)
    session = server.WebSession()

    session.slots = []
    assert session.set_permission_mode("auto") is None
    assert session.multi_agent is True
    session.set_multi_agent(False)
    assert session.set_permission_mode("auto") is None
    assert session.multi_agent is True
    session.web_state["effort"] = "medium"
    server._save_web_state(session.web_state)
    restored = server.WebSession()

    assert restored.permission_mode.value == "auto"
    assert restored.multi_agent is True
    assert restored.effort == "medium"


def test_desktop_session_loads_verification_agent(monkeypatch, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _patch_web_files(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "DESKTOP_VERIFICATION_AGENT_ENABLED", True)
    monkeypatch.setattr("mewcode.memory.auto_memory.MemoryManager", lambda _path: None)
    monkeypatch.chdir(project)

    session = server.WebSession()
    agents = dict(session.context.get(server.AGENT_CATALOG).list_agents())
    session.permission_mode = server.PermissionMode.AUTO
    session.multi_agent = True
    session._build_agent(ProviderConfig(
        name="test",
        protocol="openai-compat",
        base_url="https://example.invalid/v1",
        model="test-model",
        api_key="test-key",
    ))

    assert "Verification" in agents
    assert session.agent is not None
    assert session.agent.registry.is_enabled("Agent")
    assert "Verification" in session.agent._agent_catalog


def test_desktop_slot_keeps_registry_key_during_scan(monkeypatch, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _patch_web_files(monkeypatch, tmp_path)
    monkeypatch.chdir(project)
    session = server.WebSession()
    provider = ProviderConfig(
        name="DeepSeek account",
        protocol="openai-compat",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
        api_key="secret",
    )
    record = SimpleNamespace(
        key="desktop:stable-profile-id",
        provider=provider,
        source="desktop",
        effort_levels=[],
        default_effort="high",
        models=["deepseek-v4-pro"],
        model_efforts={"deepseek-v4-pro": ["low", "high", "max"]},
    )
    monkeypatch.setattr(server, "discover_provider_slots", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(server.MODEL_REGISTRY, "refresh_discovered", lambda _slots: None)
    monkeypatch.setattr(server.MODEL_REGISTRY, "records", lambda: [record])
    monkeypatch.setattr(session, "_maybe_mirror_cc_switch", lambda: None)
    applied_efforts = []
    session.slot = server.ProviderSlot(
        provider=provider,
        source="desktop",
        registry_key="desktop:stable-profile-id",
    )
    session.client = SimpleNamespace(
        effort="",
        set_effort=applied_efforts.append,
    )

    session.scan_slots()

    assert session.slots[0].key == "desktop:stable-profile-id"
    assert session.slots[0].effort_levels == ["low", "high", "max"]
    assert session.slots[0].default_effort == "high"
    assert session.slots[0].models == ["deepseek-v4-pro"]
    assert session.slot.effort_levels == ["low", "high", "max"]
    assert applied_efforts == ["high"]
    session._close_session()


async def _noop_resolve_window(_provider: ProviderConfig) -> None:
    return None


@pytest.mark.asyncio
async def test_model_ref_switch_reports_the_same_stable_provider_key(monkeypatch) -> None:
    provider = ProviderConfig(
        name="Editable DeepSeek name",
        protocol="openai-compat",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
        api_key="secret",
    )
    primary = SimpleNamespace(get_usage_recorder=lambda: None)
    auxiliary = SimpleNamespace()
    record = SimpleNamespace(
        key="desktop:stable-profile-id",
        source="desktop",
        label="DeepSeek",
        effort_levels=["low", "high"],
    )
    resolved = SimpleNamespace(
        provider=record,
        config=provider,
        client=primary,
        effort="high",
        protocol="openai-compat",
        context_window=1_000_000,
    )
    configured = []
    remembered = []
    saved = []
    session = server.WebSession.__new__(server.WebSession)
    session.slot = None
    session.client = None
    session.effort = ""
    session.web_state = {}
    session._agent_tool = None
    session.agent = SimpleNamespace(
        client=primary,
        configure_model=lambda *args, **kwargs: configured.append((args, kwargs)),
        auxiliary_client=None,
        safety_reviewer=SimpleNamespace(client=None),
        registry=SimpleNamespace(get=lambda _name: None),
    )
    session._apply_context_tier = lambda: None
    session._remember_provider = remembered.append
    session._save_active_conversation_settings = lambda: saved.append(True)
    session._resolve_window = _noop_resolve_window
    monkeypatch.setattr(server.MODEL_REGISTRY, "resolve", lambda _ref: resolved)
    monkeypatch.setattr(server, "_build_auxiliary_client", lambda _provider: auxiliary)
    monkeypatch.setattr("mewcode.permissions.review_client.load_review_model_settings", lambda *_: {})

    error = await session.use_model_ref(
        "desktop:stable-profile-id", "deepseek-v4-pro", "high"
    )

    assert error is None
    assert session.slot.key == "desktop:stable-profile-id"
    assert remembered == ["desktop:stable-profile-id"]
    assert saved == [True]
    assert configured
    assert session.agent.auxiliary_client is auxiliary
    assert session.agent.safety_reviewer.client is auxiliary


def test_web_permission_menu_exposes_three_levels_and_plan() -> None:
    assert [choice["value"] for choice in server.PERMISSION_MODE_CHOICES] == [
        "default",
        "plan",
        "auto",
        "bypassPermissions",
    ]


def test_cache_status_distinguishes_unknown_zero_and_hit(
    monkeypatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _patch_web_files(monkeypatch, tmp_path)
    monkeypatch.chdir(project)
    session = server.WebSession()
    agent = SimpleNamespace(
        context_window=272_000,
        permission_mode=session.permission_mode,
        total_input_tokens=100,
        total_output_tokens=5,
        total_cache_read_tokens=0,
        total_cache_creation_tokens=0,
        cache_usage_available=False,
    )
    session.agent = agent

    unknown = session.status_payload()
    assert unknown["cache_usage_available"] is False
    assert unknown["cache_hit_rate"] is None

    agent.cache_usage_available = True
    known_zero = session.status_payload()
    assert known_zero["cache_usage_available"] is True
    assert known_zero["cache_hit_rate"] == 0.0

    agent.total_cache_read_tokens = 900
    hit = session.status_payload()
    assert hit["cache_hit_rate"] == 0.9
    session._close_session()


def test_deepseek_cache_observability_payload_exposes_epoch_and_request() -> None:
    observation = SimpleNamespace(
        cache_hit_tokens=900,
        cache_miss_tokens=100,
        cache_hit_rate=0.9,
        cache_usage_available=True,
        request_cost_usd=0.00045,
        invalidation_reason="same_shape_server_miss",
        system_hash="system-sha",
        tools_hash="tools-sha",
        longest_common_prefix_tokens=777,
    )
    agent = SimpleNamespace(
        is_deepseek_context=True,
        total_input_tokens=300,
        cache_observations=[observation],
        cache_epoch=4,
        total_deepseek_cache_hit_tokens=2_700,
        total_deepseek_cache_miss_tokens=300,
        cache_usage_available=True,
        total_estimated_cost_usd=0.00123,
    )

    payload = server._cache_observability_payload(agent)

    assert payload == {
        "cache_miss": 300,
            "cache_request_read": 900,
            "cache_request_miss": 100,
            "cache_request_creation": 0,
            "cache_request_hit_rate": 0.9,
        "cache_request_usage_available": True,
            "cache_cumulative_read": 2_700,
            "cache_cumulative_miss": 300,
            "cache_cumulative_creation": 0,
            "cache_cumulative_hit_rate": 0.9,
            "cache_cumulative_usage_available": True,
            "cache_slot": "",
            "cache_provider": "",
            "cache_type": "",
        "cache_epoch": 4,
        "cache_invalidation_reason": "same_shape_server_miss",
        "cache_request_cost_usd": 0.00045,
        "cache_cost_usd": 0.00123,
        "cache_system_hash": "system-sha",
        "cache_tools_hash": "tools-sha",
        "cache_lcp_tokens": 777,
    }


def test_gpt_cache_observability_uses_openai_slot_totals() -> None:
    provider = resolve_cache_usage_provider("generic", "openai")
    totals = CacheTotals(provider.slot)
    totals.record(normalized_cache_usage(
        input_tokens=100,
        cache_read_tokens=900,
        cache_creation_tokens=50,
        available=True,
    ))
    observation = SimpleNamespace(
        cache_hit_tokens=900,
        cache_miss_tokens=100,
        cache_creation_tokens=50,
        cache_hit_rate=900 / 1_050,
        cache_usage_available=True,
        request_cost_usd=0.0,
        invalidation_reason="",
        system_hash="system-sha",
        tools_hash="tools-sha",
        longest_common_prefix_tokens=800,
    )
    agent = SimpleNamespace(
        cache_usage_provider=provider,
        cache_totals=totals,
        cache_observations=[observation],
        cache_epoch=1,
        total_estimated_cost_usd=0.0,
    )

    payload = server._cache_observability_payload(agent)

    assert payload["cache_provider"] == "openai"
    assert payload["cache_slot"] == "openai_prompt"
    assert payload["cache_cumulative_read"] == 900
    assert payload["cache_cumulative_creation"] == 50
    assert payload["cache_cumulative_hit_rate"] == 900 / 1_050


def test_deepseek_cache_epoch_does_not_turn_missing_metrics_into_zero() -> None:
    observation = SimpleNamespace(
        cache_hit_tokens=0,
        cache_miss_tokens=0,
        cache_hit_rate=0.0,
        cache_usage_available=False,
        request_cost_usd=0.0,
        invalidation_reason="",
        system_hash="system-sha",
        tools_hash="tools-sha",
        longest_common_prefix_tokens=0,
    )
    agent = SimpleNamespace(
        is_deepseek_context=True,
        cache_observations=[observation],
        cache_epoch=1,
        cache_usage_available=False,
        total_deepseek_cache_hit_tokens=0,
        total_deepseek_cache_miss_tokens=0,
        total_estimated_cost_usd=0.0,
    )

    payload = server._cache_observability_payload(agent)

    assert payload["cache_request_usage_available"] is False
    assert payload["cache_request_hit_rate"] is None
    assert payload["cache_cumulative_usage_available"] is False
    assert payload["cache_cumulative_hit_rate"] is None


def test_web_session_migrates_legacy_internal_mode(monkeypatch, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _patch_web_files(monkeypatch, tmp_path)
    server.STATE_FILE.write_text(
        json.dumps({"permission_mode": "dontAsk"}), encoding="utf-8"
    )
    monkeypatch.chdir(project)

    session = server.WebSession()

    assert session.permission_mode.value == "default"
    assert json.loads(server.STATE_FILE.read_text(encoding="utf-8"))["permission_mode"] == "default"


def test_switching_projects_detaches_old_project_conversation(
    monkeypatch, tmp_path: Path
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _patch_web_files(monkeypatch, tmp_path)
    monkeypatch.chdir(first)
    session = server.WebSession()
    session.new_conversation("project")

    assert session.activate_project(str(second)) is None

    assert session.work_dir == str(second)
    assert session.session is None
    assert session.conversation.history == []


def test_completed_task_is_injected_into_its_active_conversation(
    monkeypatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _patch_web_files(monkeypatch, tmp_path)
    monkeypatch.chdir(project)
    session = server.WebSession()
    created = session.new_conversation("project")
    task = BackgroundTask(
        id="task-active",
        name="worker",
        agent=None,
        task="inspect",
        status="completed",
        result="found it",
        owner_session_id=created["id"],
        owner_store_dir=str(project),
    )
    session.task_manager._tasks[task.id] = task
    session.task_manager.requeue_notification(task.id)

    active = session.collect_task_notifications()

    assert [item.id for item in active] == [task.id]
    assert session._has_task_notification(session.conversation.history, task.id)
    assert session.task_manager.get(task.id).notified is True
    session._close_session()


def test_completed_expert_child_does_not_start_generic_followup(
    monkeypatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _patch_web_files(monkeypatch, tmp_path)
    monkeypatch.chdir(project)
    session = server.WebSession()
    created = session.new_conversation("project")
    task = BackgroundTask(
        id="task-expert", name="expert child", agent=None, task="inspect",
        status="completed", result="checkpoint result",
        owner_session_id=created["id"], owner_store_dir=str(project),
        origin="expert",
    )
    session.task_manager._tasks[task.id] = task
    session.task_manager.requeue_notification(task.id)

    active = session.collect_task_notifications()

    assert active == []
    assert session._has_task_notification(session.conversation.history, task.id) is False
    assert session.task_manager.get(task.id).notified is True
    session._close_session()


def test_completed_task_is_written_back_to_inactive_conversation(
    monkeypatch, tmp_path: Path
) -> None:
    active_project = tmp_path / "active"
    other_project = tmp_path / "other"
    active_project.mkdir()
    other_project.mkdir()
    _patch_web_files(monkeypatch, tmp_path)
    monkeypatch.chdir(active_project)
    session = server.WebSession()
    session.new_conversation("project")

    other_manager = SessionManager(str(other_project))
    other = other_manager.create()
    other_id = other.session_id
    other.close()
    task = BackgroundTask(
        id="task-inactive",
        name="worker",
        agent=None,
        task="inspect elsewhere",
        status="completed",
        result="done elsewhere",
        owner_session_id=other_id,
        owner_store_dir=str(other_project),
    )
    session.task_manager._tasks[task.id] = task
    session.task_manager.requeue_notification(task.id)

    assert session.collect_task_notifications() == []
    restored = other_manager.resume(other_id)
    assert restored is not None
    assert session._has_task_notification(restored.messages, task.id)
    restored.session.close()
    session._close_session()


def test_persisted_task_appears_in_background_panel(
    monkeypatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _patch_web_files(monkeypatch, tmp_path)
    monkeypatch.chdir(project)
    session = server.WebSession()
    created = session.new_conversation("project")
    task = BackgroundTask(
        id="task-panel",
        name="durable worker",
        agent=None,
        task="retain this result",
        status="completed",
        result="durable result",
        owner_session_id=created["id"],
        owner_store_dir=str(project),
    )
    session.task_manager._tasks[task.id] = task

    payload = session.agent_tasks_payload()

    assert payload["groups"][0]["rows"][0]["task_id"] == task.id
    assert payload["groups"][0]["rows"][0]["result_available"] is True
    session._close_session()


def test_background_panel_clock_excludes_old_completed_task(
    monkeypatch, tmp_path: Path
) -> None:
    import time

    project = tmp_path / "project"
    project.mkdir()
    _patch_web_files(monkeypatch, tmp_path)
    monkeypatch.chdir(project)
    session = server.WebSession()
    created = session.new_conversation("project")
    now = time.monotonic()
    common = {"owner_session_id": created["id"], "owner_store_dir": str(project)}
    old = BackgroundTask(
        id="old", name="old verification", agent=None, task="old",
        status="completed", start_time=now - 3600, end_time=now - 60,
        **common,
    )
    current = BackgroundTask(
        id="current", name="new verification", agent=None, task="new",
        status="running", start_time=now - 3, **common,
    )
    session.task_manager._tasks.update({old.id: old, current.id: current})

    payload = session.agent_tasks_payload()

    group = payload["groups"][0]
    assert group["status"] == "running"
    assert 2 <= group["elapsed"] < 10
    assert next(row for row in group["rows"] if row["task_id"] == "old")["elapsed"] > 3500
    current.status = "completed"
    current.end_time = time.monotonic()
    assert session.agent_tasks_payload()["groups"][0]["elapsed"] == 0
    session._close_session()


def test_background_panel_queued_child_has_no_group_runtime(
    monkeypatch, tmp_path: Path
) -> None:
    import time

    project = tmp_path / "project"
    project.mkdir()
    _patch_web_files(monkeypatch, tmp_path)
    monkeypatch.chdir(project)
    session = server.WebSession()
    created = session.new_conversation("project")
    agent = SimpleNamespace(
        agent_id="queued-child", total_input_tokens=0,
        total_output_tokens=0, total_tool_calls=0,
    )
    session._agent_tool = SimpleNamespace(runs={agent.agent_id: {
        "agent": agent, "name": "queued verification", "agent_type": "Verification",
        "mode": "bg", "status": "running", "task": "read-only review",
        "task_id": "", "team": "", "start": time.monotonic() - 3540,
        "end": None, "owner_session_id": created["id"],
        "owner_store_dir": str(project),
    }})
    monkeypatch.setattr(server.APP_RUNTIME_SCHEDULER, "queue_position", lambda **_: 1)

    group = session.agent_tasks_payload()["groups"][0]

    assert group["rows"][0]["status"] == "queued"
    assert group["elapsed"] == 0
    session._close_session()


@pytest.mark.asyncio
async def test_child_clock_does_not_jump_from_old_spawn_time_when_first_admitted(
    monkeypatch, tmp_path: Path
) -> None:
    import time
    from mewcode.runtime_scheduler import RuntimeScheduler

    scheduler = RuntimeScheduler(limit=1)
    monkeypatch.setattr(server, "APP_RUNTIME_SCHEDULER", scheduler)
    project = tmp_path / "project"
    project.mkdir()
    _patch_web_files(monkeypatch, tmp_path)
    monkeypatch.chdir(project)
    session = server.WebSession()
    created = session.new_conversation("project")
    agent = SimpleNamespace(agent_id="delayed-child", total_input_tokens=0,
                            total_output_tokens=0, total_tool_calls=0)
    session._agent_tool = SimpleNamespace(runs={agent.agent_id: {
        "agent": agent, "name": "delayed child", "agent_type": "Verification",
        "mode": "bg", "status": "running", "task": "read-only review",
        "task_id": "", "team": "", "start": time.monotonic() - 3540,
        "end": None, "owner_session_id": created["id"],
        "owner_store_dir": str(project),
    }})
    before = session.agent_tasks_payload()["groups"][0]
    assert before["rows"][0]["elapsed"] == 0
    assert before["rows"][0]["runtime_started"] is False
    waiter = await scheduler.acquire(kind="model", agent_id=agent.agent_id)
    after = session.agent_tasks_payload()["groups"][0]
    assert after["rows"][0]["runtime_started"] is True
    assert after["rows"][0]["elapsed"] < 5
    assert after["elapsed"] < 5
    await scheduler.release(waiter)
    session._close_session()
