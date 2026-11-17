"""Confirming an orchestration must actually start the expert-team run."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from frontend import server
from mewcode.experts.flow import ExpertFlowStore
from mewcode.orchestration import OrchestrationSpec, get_template
from mewcode.orchestration.plan_session import PlanSessionStore
from mewcode.permissions import PermissionMode


def _bound_spec(goal: str) -> OrchestrationSpec:
    raw = get_template("linear", goal=goal).as_dict()
    for node in raw["nodes"]:
        if node.get("kind") == "gate":
            continue
        node["model"] = {"provider_key": "p", "model_id": "m-test"}
        brief = node.setdefault("plan_brief", {})
        brief.setdefault("summary", node.get("goal") or node.get("label"))
        brief["open_questions"] = []
    return OrchestrationSpec.from_value(raw)


@pytest.fixture
def confirm_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate stores and capture whatever confirm tries to launch."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(server.SESSION, "session", SimpleNamespace(session_id="confirm-test"))
    monkeypatch.setattr(server.SESSION, "work_dir", str(project))
    monkeypatch.setattr(
        server, "get_plan_session_store", lambda: PlanSessionStore(tmp_path / "plans.json")
    )
    flow_store = ExpertFlowStore(tmp_path / "flows.json")
    monkeypatch.setattr(server, "_conversation_flow_store", lambda _id=None: flow_store)
    monkeypatch.setattr(server.EXPERT_RUNS, "latest_resumable", lambda _id: None)
    monkeypatch.setattr(server.SESSION, "chat_busy", lambda: False)
    monkeypatch.setattr(server.MODEL_REGISTRY, "resolve", lambda ref: SimpleNamespace(ref=ref))
    monkeypatch.setattr(server.SESSION, "set_orchestration_planning", lambda on: None)

    class FakeAgent:
        def __init__(self) -> None:
            self.permission_mode = PermissionMode.DEFAULT
            self.permission_checker = SimpleNamespace(mode=PermissionMode.DEFAULT)

        def set_permission_mode(self, mode) -> None:
            pm = mode if isinstance(mode, PermissionMode) else PermissionMode(mode)
            self.permission_mode = pm
            self.permission_checker.mode = pm

    class FakeRuntime:
        expert_mode = True
        conversation_id = "confirm-test"

        def __init__(self) -> None:
            self.agent = FakeAgent()

        def running(self) -> bool:
            return False

    class Started(list):
        runtime: FakeRuntime

    started = Started()
    runtime = FakeRuntime()
    started.runtime = runtime

    async def fake_start_runtime_turn(runtime, text, **kwargs):
        started.append(text)
    monkeypatch.setattr(server, "_save_web_state", lambda *_a, **_k: None)
    monkeypatch.setattr(
        server.SESSION, "_save_active_conversation_settings", lambda: None
    )
    monkeypatch.setattr(server.SESSION, "_save_runtime_settings", lambda _runtime: None)
    monkeypatch.setattr(server, "start_runtime_turn", fake_start_runtime_turn)
    monkeypatch.setattr(
        server.SESSION, "capture_conversation_runtime", lambda: runtime
    )
    monkeypatch.setattr(server.SESSION, "set_expert_mode",
                        lambda on, **_kwargs: setattr(server.SESSION, "expert_mode", bool(on)))
    monkeypatch.setattr(
        server.SESSION, "set_permission_mode", lambda mode: None
    )
    monkeypatch.setattr(server.SESSION, "expert_mode", True, raising=False)
    return started


@pytest.mark.asyncio
async def test_confirm_blocks_unresolved_inputs_before_any_run(confirm_env):
    raw = _bound_spec("科研绘图").as_dict()
    raw["nodes"][0].setdefault("plan_brief", {})["known_inputs"] = ["既有阶段 topic_plan.known_inputs 中的 Topic"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.post("/api/orchestration/confirm", json={"spec": raw, "activate": True})
    assert response.status_code == 400
    assert "未解析" in response.json()["error"]
    assert confirm_env == []


@pytest.mark.asyncio
async def test_confirm_with_activate_starts_the_run(confirm_env) -> None:
    started = confirm_env
    spec = _bound_spec("把编排流跑起来")
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/orchestration/confirm",
            json={"spec": spec.as_dict(), "activate": True},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["confirmed"] is True
    assert payload["started"] is True
    assert payload["run_prompt"] == "把编排流跑起来"
    assert started == ["把编排流跑起来"]


@pytest.mark.asyncio
async def test_host_bound_new_task_does_not_read_unrelated_run_histories(confirm_env, monkeypatch):
    from contextlib import nullcontext
    from mewcode.orchestration.task_design import TaskDesign
    raw = _bound_spec('Start the separately bound successor').as_dict()
    raw.setdefault('meta',{})['task_design'] = {'task_id':'a'*32}
    confirm_env.runtime.work_dir = server.SESSION.work_dir
    monkeypatch.setattr(TaskDesign,'dispatch_guard',lambda *_:nullcontext())
    def unrelated_history(*_):
        raise AssertionError('A new host claim must not hydrate previous task histories')
    monkeypatch.setattr(server.EXPERT_RUNS,'latest_resumable',unrelated_history)
    monkeypatch.setattr(server.EXPERT_RUNS,'load',unrelated_history)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://test') as client:
        response = await client.post('/api/orchestration/confirm',json=dict(spec=raw,activate=True,
            task_design_dispatch={'task_id':'a'*32,'claim_id':'b'*32}))
    assert response.status_code == 200,response.text
    assert response.json()['started'] is True
    assert confirm_env == ['Start the separately bound successor']


@pytest.mark.asyncio
async def test_confirm_with_ui_role_bindings_starts_without_relocating_fixed_nodes(confirm_env,monkeypatch):
    """The real confirm dialog sends bindings even when nodes already have them."""
    from mewcode.model_registry import ModelRef
    raw=_bound_spec("带模型选择开跑").as_dict()
    binding={"provider_key":"ui-provider","model_id":"ui-model","effort":"xhigh"}
    captured=[]
    async def start(runtime,text,**kwargs):
        captured.append(kwargs["confirmed_new_flow"])
        confirm_env.append(text)
    def forbidden_relocation(_ref):
        raise AssertionError("Activation must preserve selected routes.")
    monkeypatch.setattr(server,"start_runtime_turn",start)
    monkeypatch.setattr(server.MODEL_REGISTRY,"relocate_model_ref",forbidden_relocation)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url="http://test") as client:
        response=await client.post("/api/orchestration/confirm",json={"spec":raw,"activate":True,
            "bindings":{role:binding for role in ("manager","planner","executor","auditor")}})
    assert response.status_code==200, response.text
    assert response.json()["started"] is True and len(captured)==1
    expected={n["node_id"]:ModelRef.from_value(n["model"]) for n in raw["nodes"] if n.get("model")}
    assert {n.node_id:n.model for n in captured[0].nodes if n.model}==expected


@pytest.mark.asyncio
async def test_ui_role_binding_fills_empty_node_and_preserves_later_stage_input(confirm_env,monkeypatch):
    from mewcode.model_registry import ModelRef
    raw=_bound_spec("绑定后执行并保留后续选择").as_dict()
    planning=next(n for n in raw["nodes"] if n["kind"]=="plan")
    planning.pop("model")
    last=raw["nodes"][-1]["node_id"]
    raw["goal_contract"]={"run_inputs":[{"name":"later_choice","required":True,"collect_at":last,
        "options":[{"value":"keep","label":"保留候选"},{"value":"publish","label":"正式发布"}]}]}
    binding={"provider_key":"chosen-provider","model_id":"chosen-model","effort":"xhigh"}
    captured=[]
    async def start(runtime,text,**kwargs):
        captured.append(kwargs["confirmed_new_flow"])
        confirm_env.append(text)
    monkeypatch.setattr(server,"start_runtime_turn",start)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url="http://test") as client:
        response=await client.post("/api/orchestration/confirm",json={"spec":raw,"activate":True,
            "bindings":{"planner":binding}})
    assert response.status_code==200, response.text
    assert captured[0].node_map[planning["node_id"]].model==ModelRef.from_value(binding)
    assert captured[0].meta["run_input_contract"][0]["collect_at"]==last
    assert len(confirm_env)==1


@pytest.mark.asyncio
async def test_confirm_rejects_unavailable_selected_route_before_mode_or_run_changes(confirm_env,monkeypatch):
    spec=_bound_spec("使用已选模型")
    def unavailable(ref):
        raise ValueError("selected provider is unavailable")
    monkeypatch.setattr(server.MODEL_REGISTRY,"resolve",unavailable)
    old_mode=server.SESSION.expert_mode
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url="http://test") as client:
        response=await client.post("/api/orchestration/confirm",json={"spec":spec.as_dict(),"activate":True})
    assert response.status_code == 400 and "selected provider is unavailable" in response.json()["error"]
    assert confirm_env == [] and server.SESSION.expert_mode == old_mode


@pytest.mark.asyncio
async def test_confirm_rejects_effort_downgrade_before_start(confirm_env,monkeypatch):
    from mewcode.model_registry import ModelRef
    raw=_bound_spec("不自动降档").as_dict()
    next(node for node in raw["nodes"] if node.get("model"))["model"]["effort"]="max"
    monkeypatch.setattr(server.MODEL_REGISTRY,"resolve",lambda ref:SimpleNamespace(ref=ModelRef(ref.provider_key,ref.model_id,"high")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url="http://test") as client:
        response=await client.post("/api/orchestration/confirm",json={"spec":raw,"activate":True})
    assert response.status_code == 400 and "未自动降档" in response.json()["error"]
    assert confirm_env == []


@pytest.mark.asyncio
async def test_confirm_collects_declared_runtime_values_before_start(confirm_env) -> None:
    raw = _bound_spec("研究论文").as_dict()
    raw["goal_contract"] = {"run_inputs": [
        {"name": "主题", "required": True, "description": "研究主题"},
        {"name": "篇数", "required": False, "default": "8"},
    ]}
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.post("/api/orchestration/confirm",
                                    json={"spec": raw, "activate": True})
        assert missing.status_code == 400
        assert missing.json()["error"] == "缺少必填运行参数：主题；未开跑"
        assert confirm_env == []
        started = await client.post("/api/orchestration/confirm",
                                    json={"spec": raw, "activate": True,
                                          "run_inputs": {"主题": "图学习", "篇数": "12"}})
    assert started.status_code == 200
    assert started.json()["started"] is True
    assert "图学习" in confirm_env[0] and '"篇数": "12"' in confirm_env[0]
    assert started.json()["run_prompt"] == confirm_env[0]


@pytest.mark.asyncio
async def test_confirm_without_activate_does_not_start(confirm_env) -> None:
    started = confirm_env
    spec = _bound_spec("只保存不开跑")
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/orchestration/confirm",
            json={"spec": spec.as_dict(), "activate": False},
        )
    assert response.status_code == 200
    assert response.json()["started"] is False
    assert started == []


@pytest.mark.asyncio
@pytest.mark.parametrize("blocking", [False, True])
async def test_confirm_only_blocks_explicit_goal_questions_and_not_assumptions(confirm_env, blocking):
    raw = _bound_spec("明确研究目标").as_dict()
    raw["goal_contract"] = {"assumptions": ["默认先检索"], "open_assumptions": ["封面样式事后查看"],
                            "run_inputs": [{"name": "主题", "default": "图学习", "required": True}]}
    raw.setdefault("meta", {})["workflow_draft"] = {"blocking_questions": [
        {"node_id": "goal", "label": "总目标", "question": "需要交付论文还是报告？", "blocking": blocking}]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.post("/api/orchestration/confirm", json={"spec": raw, "activate": True})
    assert response.status_code == 200, response.text
    assert response.json()["needs_clarify"] is blocking
    assert response.json()["started"] is not blocking
    assert len(confirm_env) == (0 if blocking else 1)


@pytest.mark.asyncio
async def test_confirm_with_open_questions_clarifies_instead_of_starting(
    confirm_env,
) -> None:
    started = confirm_env
    raw = _bound_spec("先问清再开跑").as_dict()
    raw["nodes"][0].setdefault("plan_brief", {})["open_questions"] = ["论文地址在哪？"]
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/orchestration/confirm",
            json={"spec": raw, "activate": True},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["needs_clarify"] is True
    assert payload["started"] is False
    assert "论文地址" in payload["clarify_prompt"]
    assert started == []


@pytest.mark.asyncio
async def test_confirm_does_not_queue_an_unbound_plan_while_a_turn_is_running(
    confirm_env, monkeypatch
) -> None:
    started = confirm_env

    class BusyRuntime:
        expert_mode = True
        conversation_id = "confirm-test"

        def running(self) -> bool:
            return True

    monkeypatch.setattr(
        server.SESSION, "capture_conversation_runtime", lambda: BusyRuntime()
    )
    spec = _bound_spec("排队等当前回合")
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/orchestration/confirm",
            json={"spec": spec.as_dict(), "activate": True},
        )
    assert response.status_code == 409
    assert "暂停或等待完成" in response.json()["error"]
    assert started == []


@pytest.mark.asyncio
async def test_confirm_reports_unreadiness_instead_of_500(
    confirm_env, monkeypatch
) -> None:
    def boom():
        raise RuntimeError("当前对话尚未准备好")

    monkeypatch.setattr(server.SESSION, "capture_conversation_runtime", boom)
    spec = _bound_spec("对话还没就绪")
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/orchestration/confirm",
            json={"spec": spec.as_dict(), "activate": True},
        )
    assert response.status_code == 400
    assert "尚未准备好" in response.json()["error"]


def test_conversation_runtime_exposes_owner_context() -> None:
    """run_expert_chat reads session.context; ConversationRuntime must proxy it."""
    owner = SimpleNamespace(context={"expert": "flows"})
    runtime = object.__new__(server.ConversationRuntime)
    runtime.owner = owner
    assert runtime.context is owner.context


def test_set_expert_mode_syncs_running_runtime(monkeypatch, tmp_path: Path) -> None:
    """Confirm must flip runtime.expert_mode even when a turn is already running."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(server.SESSION, "work_dir", str(project))
    monkeypatch.setattr(server.SESSION, "expert_mode", False, raising=False)

    class RunningRuntime:
        expert_mode = False
        orchestration_planning = True

        def running(self) -> bool:
            return True

    runtime = RunningRuntime()
    monkeypatch.setattr(
        server.SESSION, "runtime_for", lambda conversation_id="": runtime
    )
    monkeypatch.setattr(
        server.SESSION,
        "conversation_runs",
        {"conv": runtime},
        raising=False,
    )
    # Skip profile / registry checks — we only care about runtime sync.
    fake_ref = SimpleNamespace()
    fake_profile = SimpleNamespace(
        manager=fake_ref, planner=None, executor=fake_ref, auditor=fake_ref
    )
    monkeypatch.setattr(
        server,
        "EXPERT_PROFILES",
        SimpleNamespace(get=lambda _work_dir: fake_profile),
    )
    monkeypatch.setattr(server.MODEL_REGISTRY, "resolve", lambda _ref: None)
    monkeypatch.setattr(server.SESSION, "set_orchestration_planning", lambda on: None)
    monkeypatch.setattr(server.SESSION, "_save_active_conversation_settings", lambda: None)
    monkeypatch.setattr(server, "_save_web_state", lambda _state: None)

    error = server.SESSION.set_expert_mode(True)
    assert error is None
    assert server.SESSION.expert_mode is True
    assert runtime.expert_mode is True
    assert runtime.orchestration_planning is False


def test_set_expert_mode_relocates_stale_provider_model_pair(monkeypatch, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    from mewcode.expert_group import ExpertProfile, ExpertProfileStore
    from mewcode.model_registry import ModelRef, ProviderRegistry

    store = ExpertProfileStore(tmp_path / "expert-profiles.json")
    registry = ProviderRegistry()
    registry.replace_desktop_profiles([
        {
            "id": "desktop:example_provider_1",
            "name": "desktop:example_provider_1",
            "brand": "custom",
            "protocol": "openai-compat",
            "base_url": "https://relay.example/v1",
            "default_model": "gpt-6-astra",
            "models": ["gpt-5.5", "gpt-5.6-sol", "gpt-6-astra"],
            "api_key": "secret",
        },
        {
            "id": "grok-main",
            "name": "desktop:example_provider_2",
            "brand": "xai",
            "api_key": "xai-secret",
        },
    ])
    stale = ModelRef("desktop:example_provider_1", "grok-4.6", "xhigh")
    store.put(project, ExpertProfile(manager=stale, executor=stale, auditor=stale))
    monkeypatch.setattr(
        "mewcode.model_registry.create_client",
        lambda _config: SimpleNamespace(
            set_effort=lambda *_args, **_kwargs: None,
            set_usage_recorder=lambda *_args, **_kwargs: None,
        ),
    )
    monkeypatch.setattr(server, "EXPERT_PROFILES", store)
    monkeypatch.setattr(server, "MODEL_REGISTRY", registry)
    monkeypatch.setattr(server.SESSION, "work_dir", str(project))
    monkeypatch.setattr(server.SESSION, "expert_mode", False, raising=False)
    monkeypatch.setattr(server.SESSION, "web_state", {"expert_mode": False}, raising=False)
    monkeypatch.setattr(server.SESSION, "conversation_runs", {}, raising=False)
    monkeypatch.setattr(server.SESSION, "set_orchestration_planning", lambda on: None)
    monkeypatch.setattr(server.SESSION, "_save_active_conversation_settings", lambda: None)
    monkeypatch.setattr(server, "_save_web_state", lambda _state: None)

    error = server.SESSION.set_expert_mode(True)
    assert error is None
    assert server.SESSION.expert_mode is True
    saved = store.get(project)
    assert saved is not None
    assert saved.manager.provider_key == "desktop:grok-main"
    assert saved.executor.model_id == "grok-4.6"
    assert saved.auditor.effort == "xhigh"


def test_capture_reused_runtime_picks_up_expert_mode(monkeypatch, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    session = server.SESSION
    monkeypatch.setattr(session, "work_dir", str(project))
    monkeypatch.setattr(session, "expert_mode", True, raising=False)
    monkeypatch.setattr(session, "orchestration_planning", False, raising=False)
    monkeypatch.setattr(session, "session", SimpleNamespace(session_id="conv"), raising=False)
    monkeypatch.setattr(session, "agent", object(), raising=False)
    monkeypatch.setattr(session, "client", object(), raising=False)
    monkeypatch.setattr(session, "agent_runtime", object(), raising=False)

    class RunningRuntime:
        expert_mode = False
        orchestration_planning = True

        def running(self) -> bool:
            return True

    runtime = RunningRuntime()
    monkeypatch.setattr(session, "conversation_runs", {"conv": runtime}, raising=False)

    captured = session.capture_conversation_runtime()
    assert captured is runtime
    assert captured.expert_mode is True
    assert captured.orchestration_planning is False


@pytest.mark.asyncio
async def test_confirm_run_defaults_permission_mode_to_current_level(confirm_env, monkeypatch) -> None:
    started = confirm_env
    monkeypatch.setattr(server.SESSION, "permission_mode", PermissionMode.DEFAULT)
    spec = _bound_spec("开跑默认自动权限")
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/orchestration/confirm",
            json={"spec": spec.as_dict(), "activate": True},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["permission_mode"] == "default"
    assert started.runtime.agent.permission_mode.value == "default"
    assert started.runtime.agent.permission_checker.mode.value == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["default", "auto", "bypassPermissions"])
async def test_confirm_run_honors_specified_permission_mode(confirm_env, mode) -> None:
    started = confirm_env
    spec = _bound_spec("开跑指定计划权限")
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/orchestration/confirm",
            json={
                "spec": spec.as_dict(),
                "activate": True,
                "permission_mode": mode,
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["permission_mode"] == mode
    assert started.runtime.agent.permission_mode == PermissionMode(mode)


@pytest.mark.asyncio
async def test_confirm_invalid_permission_mode_falls_back_to_authorized(confirm_env) -> None:
    started = confirm_env
    spec = _bound_spec("无效权限回退自动")
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/orchestration/confirm",
            json={
                "spec": spec.as_dict(),
                "activate": True,
                "permission_mode": "not-a-mode",
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["permission_mode"] == "default"
    assert started.runtime.agent.permission_mode == PermissionMode.DEFAULT


def test_expert_child_inherits_parent_permission_mode() -> None:
    from mewcode.expert_web import _inherited_permission_mode

    session = SimpleNamespace(
        agent=SimpleNamespace(
            permission_checker=SimpleNamespace(mode=PermissionMode.AUTO),
            permission_mode=PermissionMode.AUTO,
        )
    )
    assert _inherited_permission_mode(session) is PermissionMode.AUTO

    session.agent.permission_checker = None
    session.agent.permission_mode = PermissionMode.BYPASS
    assert _inherited_permission_mode(session) is PermissionMode.BYPASS

    session.agent.permission_mode = None
    assert _inherited_permission_mode(session) is PermissionMode.AUTO


@pytest.mark.asyncio
@pytest.mark.parametrize("new_context", ["none", "mode", "conversation"])
async def test_failed_confirmation_restores_only_captured_permission_owner(confirm_env, monkeypatch, new_context):
    runtime = confirm_env.runtime
    monkeypatch.setattr(server.SESSION, "session", SimpleNamespace(session_id=runtime.conversation_id))
    monkeypatch.setattr(server.SESSION, "permission_mode", PermissionMode.PLAN)
    monkeypatch.setattr(server.SESSION, "permission_before_plan", "default")
    monkeypatch.setattr(server.SESSION, "permission_before_orch", "default")
    monkeypatch.setattr(server.SESSION, "runtime_for", lambda *_: runtime)
    saved = []
    monkeypatch.setattr(server.SESSION, "_save_runtime_settings", lambda captured: saved.append(captured))
    async def fail_start(*args, **kwargs):
        if new_context == "mode":
            server.SESSION.permission_mode = PermissionMode.BYPASS
            runtime.agent.set_permission_mode(PermissionMode.BYPASS)
        elif new_context == "conversation":
            server.SESSION.session = SimpleNamespace(session_id="other-owner")
        raise ValueError("fixture start failed")
    monkeypatch.setattr(server, "start_runtime_turn", fail_start)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.post("/api/orchestration/confirm", json={"spec": _bound_spec("回滚权限").as_dict(), "activate": True, "permission_mode": "auto"})
    assert response.status_code == 400
    assert runtime.agent.permission_mode == {"none": PermissionMode.PLAN,
        "mode": PermissionMode.BYPASS, "conversation": PermissionMode.AUTO}[new_context]
    assert saved == ([runtime] if new_context == "none" else [])
    assert server.SESSION.permission_mode == (PermissionMode.BYPASS if new_context == "mode" else PermissionMode.PLAN)
    if new_context == "none":
        assert runtime.permission_before_plan == "default"
        assert server.SESSION.permission_before_plan == "default"


def test_permission_selector_reads_current_host_mode_and_sends_selected_value() -> None:
    app = Path("frontend/static/app.js").read_text(encoding="utf-8")
    # The old orchestration confirm form was replaced by the shared selector.
    assert 'currentStatus.permission_level || currentStatus.permission_mode' in app
    assert 'send({ type: "set_permission_mode", mode: m.value })' in app
