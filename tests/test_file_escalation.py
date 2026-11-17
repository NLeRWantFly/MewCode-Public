import copy
from dataclasses import replace

import pytest

from mewcode.expert_group import (
    ExpertExecutionPolicy,
    ExpertRoleResult,
    select_capability_executor_model,
)
from mewcode.experts.file_escalation import resolve_capability_requests, resolve_file_requests
from mewcode.experts.flow import ExpertNodeSpec
from mewcode.model_registry import ModelRef
from mewcode.tools.submit_handoff import SubmitHandoffParams


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ok", "wrong", "denied", "escape", "parallel", "budget"])
async def test_parent_file_delivery_checks_receipt_content_scope_and_budget(tmp_path, mode):
    content = "原始正文\r\n  保留空格\n"
    handoff = SubmitHandoffParams(summary="请求上级落盘", status="blocked",
        blocker_kind="capability_missing", blockers=["只读节点不能写文件"],
        inline_outputs=[{"name": "overview", "content": content}],
        file_requests=[{"output_name": "overview", "path": "report/refs20/_overview.md"}]).model_dump()
    if mode == "escape":
        handoff["file_requests"][0]["path"] = "../outside.md"
    original = copy.deepcopy(handoff)
    node = ExpertNodeSpec("manager", "manager", "分析")
    policy = ExpertExecutionPolicy("stable", False, 0, token_budget=2000,
        parallel_readonly=mode == "parallel", deny_tools=("Bash",))
    if mode == "budget":
        policy = replace(policy, token_budget=100)
    result = ExpertRoleResult(text="等待落盘", status="needs_attention",
                              handoff=handoff, billable_tokens=10)
    calls, events = [], []

    async def emit(event):
        events.append(event)

    async def dispatch(child_policy, prompt):
        calls.append(child_policy)
        assert not child_policy.allow_delegation
        assert not child_policy.resume_messages
        assert child_policy.deny_tools == ("Bash",)
        assert child_policy.token_budget == 1990
        assert child_policy.stage_inputs["manager"]["handoff"] == original
        if mode == "denied":
            return ExpertRoleResult(text="permission denied", status="needs_attention")
        target = tmp_path / "report/refs20/_overview.md"
        target.parent.mkdir(parents=True)
        target.write_bytes((content if mode == "ok" else "summary").encode("utf-8"))
        return ExpertRoleResult(text="written", status="completed", billable_tokens=20,
            handoff={"status": "completed", "artifacts": [{"path": "report/refs20/_overview.md"}]})

    actual = await resolve_file_requests(node, result, policy, work_dir=tmp_path, dispatch=dispatch, emit=emit)
    assert handoff == original
    assert len(calls) == (0 if mode in {"escape", "parallel", "budget"} else 1)
    if mode == "ok":
        assert actual.status == "completed"
        assert actual.billable_tokens == 30
        assert actual.handoff["inline_outputs"] == original["inline_outputs"]
        assert actual.handoff["escalation_receipt"]["status"] == "completed"
        assert actual.handoff["artifacts"][0]["note"].startswith("sha256:")
    else:
        assert actual.status != "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_allows", [True, False])
async def test_file_helper_inherits_only_the_explicit_parent_authorization(tmp_path, parent_allows):
    content = "交付正文\n"
    handoff = SubmitHandoffParams(summary="request file", status="blocked",
        blocker_kind="capability_missing",
        inline_outputs=[{"name": "report", "content": content}],
        file_requests=[{"output_name": "report", "path": "report.md"}]).model_dump()
    node = ExpertNodeSpec("N01::exp::child", "planner", "child")
    policy = ExpertExecutionPolicy("stable", False, 0, authorization_node_id="N01")
    result = ExpertRoleResult(text="request", status="needs_attention", handoff=handoff)
    helper_policies = []

    async def emit(_event):
        return None

    async def dispatch(helper_policy, _prompt):
        helper_policies.append(helper_policy)
        assert helper_policy.authorization_node_id == "N01"
        from mewcode.kernel.capabilities import call_gap
        manifest = {"schema_version": 2, "approved": True,
            "workspace": str(tmp_path), "nodes": {"N01": {
                "write_paths": ["."] if parent_allows else [],
            }}}
        gap = call_gap(manifest, helper_policy.authorization_node_id,
            "WriteFile", {"file_path": str(tmp_path / "report.md")})
        if gap:
            return ExpertRoleResult(text="parent authorization denied", status="needs_attention")
        target = tmp_path / "report.md"
        target.write_bytes(content.encode("utf-8"))
        return ExpertRoleResult(text="written", status="completed",
            handoff={"status": "completed", "artifacts": [{"path": "report.md"}]})

    actual = await resolve_file_requests(node, result, policy, work_dir=tmp_path,
        dispatch=dispatch, emit=emit)

    assert len(helper_policies) == 1
    assert helper_policies[0].authorization_node_id == "N01"
    assert (actual.status == "completed") is parent_allows


@pytest.mark.asyncio
async def test_file_helper_sets_exact_parent_identity_when_policy_has_no_explicit_value(tmp_path):
    handoff = SubmitHandoffParams(summary="request file", status="blocked",
        blocker_kind="capability_missing",
        inline_outputs=[{"name": "report", "content": "body"}],
        file_requests=[{"output_name": "report", "path": "report.md"}]).model_dump()
    node = ExpertNodeSpec("N01", "manager", "parent")
    policy = ExpertExecutionPolicy("stable", False, 0)
    observed = []

    async def emit(_event):
        return None

    async def dispatch(helper_policy, _prompt):
        observed.append(helper_policy.authorization_node_id)
        return ExpertRoleResult(text="denied", status="needs_attention")

    await resolve_file_requests(node, ExpertRoleResult(text="request", status="needs_attention",
        handoff=handoff), policy, work_dir=tmp_path, dispatch=dispatch, emit=emit)

    assert observed == ["N01"]


def test_file_request_requires_complete_unique_source_and_internal_blocker():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SubmitHandoffParams(summary="missing content", status="blocked", blocker_kind="capability_missing",
                            file_requests=[{"output_name": "missing", "path": "report.md"}])


def command_request(command="python --version"):
    return {
        "command": command,
        "purpose": "取得 Python 环境实测值",
        "input_scope": ["当前工作区和 Python 版本信息"],
        "forbidden_changes": ["禁止写文件、改配置、访问网络或安装依赖"],
        "deliverables": ["stdout、stderr 和退出码"],
        "verification_command": command,
    }


def grok_catalog():
    return {"providers": [{
        "key": "desktop:grok-main",
        "name": "desktop:example_provider_2",
        "brand": "xai",
        "base_url": "https://api.x.ai/v1",
        "models": ["grok-4.6"],
    }]}


def test_capability_executor_requires_selection_even_when_grok_is_available():
    catalog = grok_catalog()
    catalog["providers"].extend([
        {"key": "cc-switch:claude", "name": "Claude relay", "brand": "anthropic",
         "models": ["claude-legacy-test"]},
        {"key": "relay:grok", "name": "generic relay", "brand": "custom",
         "models": ["grok-4.6"]},
    ])
    selected, reason = select_capability_executor_model(catalog)
    assert selected is None
    assert "尚未配置辅助执行器" in reason

    missing, reason = select_capability_executor_model({"providers": [
        {"key": "cc-switch:claude", "name": "Claude", "brand": "anthropic",
         "models": ["claude-legacy-test"]},
    ]})
    assert missing is None
    assert "尚未配置辅助执行器" in reason


def test_capability_executor_honors_explicit_model_and_never_falls_back():
    catalog = grok_catalog()
    catalog["providers"].append({
        "key": "cc-switch:claude", "name": "Claude relay", "brand": "anthropic",
        "models": ["claude-legacy-test"],
    })
    preferred = ModelRef(
        "cc-switch:claude", "claude-legacy-test", "medium"
    )
    selected, reason = select_capability_executor_model(catalog, preferred)
    assert selected == preferred
    assert reason == "configured capability executor"

    missing = ModelRef("missing:provider", "missing-model", "high")
    selected, reason = select_capability_executor_model(catalog, missing)
    assert selected is None
    assert "未自动替换提供方或模型" in reason


def test_command_request_requires_exact_verification_and_cannot_mix_file_delivery():
    from pydantic import ValidationError
    invalid = command_request()
    invalid["verification_command"] = "pip list"
    with pytest.raises(ValidationError, match="verification_command"):
        SubmitHandoffParams(summary="probe", status="blocked", blocker_kind="capability_missing",
                            command_requests=[invalid])
    with pytest.raises(ValidationError, match="不能与文件落盘请求混在"):
        SubmitHandoffParams(summary="mixed", status="blocked", blocker_kind="capability_missing",
            inline_outputs=[{"name": "report", "content": "body"}],
            file_requests=[{"output_name": "report", "path": "report.md"}],
            command_requests=[command_request()])


@pytest.mark.asyncio
async def test_parent_command_probe_is_exactly_scoped_and_returns_for_followup(tmp_path):
    command = "python --version"
    handoff = SubmitHandoffParams(summary="需要环境实测", status="blocked",
        blocker_kind="capability_missing", blockers=["planner 没有 Bash"],
        command_requests=[command_request(command)]).model_dump()
    node = ExpertNodeSpec("probe", "planner", "环境探测")
    policy = ExpertExecutionPolicy("stable", False, 0, token_budget=3000)
    result = ExpertRoleResult(text="等待只读探测", status="needs_attention",
                              handoff=handoff, billable_tokens=10)
    calls, events = [], []

    async def emit(event):
        events.append(event)

    async def dispatch(child_policy, prompt):
        calls.append(child_policy)
        assert set(child_policy.node_tools) == {"ReadFile", "Glob", "Grep", "Bash"}
        assert child_policy.command_allowlist == (command,)
        assert "不得改写、拼接、替换或补充命令" in prompt
        return ExpertRoleResult(text="Python 3.12.8", status="completed", billable_tokens=20,
            handoff={"status": "completed", "summary": "Python 版本已取得",
                     "evidence": [{"kind": "command", "ref": command, "result": "exit 0"}]},
            verification_records=[{"name": "Bash", "args": {"command": command},
                                   "status": "completed", "is_error": False,
                                   "output": "Python 3.12.8\nEXIT CODE: 0"}])

    actual = await resolve_capability_requests(node, result, policy, work_dir=tmp_path,
        dispatch=dispatch, emit=emit, executor_route={"provider": "grok", "model": "grok-4.6"})
    assert len(calls) == 1
    assert actual.status == "needs_attention"
    assert actual.error_kind == "capability_resolved"
    assert actual.handoff["command_requests"] == []
    assert actual.handoff["capability_followup_required"] is True
    assert actual.handoff["escalation_receipt"]["status"] == "completed"
    assert actual.handoff["escalation_receipt"]["executor_route"] == {"provider": "grok", "model": "grok-4.6"}
    assert [event["status"] for event in events] == ["running", "completed"]


@pytest.mark.asyncio
async def test_identical_failed_escalation_is_not_redispatched_to_same_executor(tmp_path):
    handoff = SubmitHandoffParams(summary="request file", status="blocked",
        blocker_kind="capability_missing", blockers=["只读"],
        inline_outputs=[{"name": "report", "content": "body"}],
        file_requests=[{"output_name": "report", "path": "report.md"}]).model_dump()
    prior = copy.deepcopy(handoff)
    prior["escalation_receipt"] = {"status": "needs_attention", "error": "403 INSUFFICIENT_BALANCE"}
    node = ExpertNodeSpec("manager", "manager", "分析")
    result = ExpertRoleResult(text="again", status="needs_attention", handoff=handoff)
    events = []

    async def dispatch(*_args):
        raise AssertionError("identical failed request must not be dispatched again")

    async def emit(event):
        events.append(event)

    actual = await resolve_capability_requests(node, result,
        ExpertExecutionPolicy("stable", False, 0), work_dir=tmp_path,
        dispatch=dispatch, emit=emit, prior_handoff=prior,
        executor_route={"provider_key": "cc-switch:cc-switch-claude", "model_id": "claude-legacy-test"},
        previous_helper_state={"request_error": "API error (403): insufficient balance\n"
                               "请求路由：Provider=cc-switch-claude; model=claude-legacy-test; URL=https://example.test"})
    assert actual.status == "needs_attention"
    assert "未原样重试" in actual.text
    assert actual.handoff["escalation_receipt"]["error"] == "403 INSUFFICIENT_BALANCE"
    assert events[-1]["deduplicated"] is True


@pytest.mark.asyncio
async def test_failed_escalation_can_retry_after_executor_route_changes(tmp_path):
    handoff = SubmitHandoffParams(summary="request file", status="blocked",
        blocker_kind="capability_missing", blockers=["只读"],
        inline_outputs=[{"name": "report", "content": "body"}],
        file_requests=[{"output_name": "report", "path": "report.md"}]).model_dump()
    prior = copy.deepcopy(handoff)
    prior["escalation_receipt"] = {"status": "needs_attention", "error": "403 INSUFFICIENT_BALANCE"}
    calls = []

    async def dispatch(*args):
        calls.append(args)
        return ExpertRoleResult(text="new route failed differently", status="needs_attention")

    async def emit(_event):
        return None

    actual = await resolve_capability_requests(ExpertNodeSpec("manager", "manager", "分析"),
        ExpertRoleResult(text="again", status="needs_attention", handoff=handoff),
        ExpertExecutionPolicy("stable", False, 0), work_dir=tmp_path,
        dispatch=dispatch, emit=emit, prior_handoff=prior,
        executor_route={"provider_key": "grok", "model_id": "grok-4.6"},
        previous_helper_state={"provider": "cc-switch-claude", "model": "claude-legacy-test"})
    assert len(calls) == 1
    assert actual.status == "needs_attention"
    assert "executor 未提交完成回执" in actual.text


@pytest.mark.asyncio
async def test_orchestrator_routes_manager_request_to_executor_and_continues(tmp_path):
    from mewcode.expert_group import ExpertGroupOrchestrator
    from mewcode.experts.flow import ExpertFlowSpec
    from tests.test_expert_flow import _profile

    flow = ExpertFlowSpec.from_value({"flow_id": "file-escalation", "label": "file delivery",
        "nodes": [{"node_id": "analyze", "role": "manager", "label": "analysis"},
                  {"node_id": "final", "role": "manager", "label": "verify"}],
        "edges": [{"from": "analyze", "to": "final"}], "finalizer_node_id": "final"})
    calls, events = [], []

    async def runner(node, model, prompt, policy, member=None):
        calls.append((node.node_id, node.role))
        if node.node_id == "analyze":
            return ExpertRoleResult(text="request", status="needs_attention", handoff=SubmitHandoffParams(
                summary="request file", status="blocked", blocker_kind="capability_missing",
                inline_outputs=[{"name": "overview", "content": "完整正文\n"}],
                file_requests=[{"output_name": "overview", "path": "overview.md"}]).model_dump())
        if node.role == "executor":
            assert model.provider_key == "desktop:grok-main"
            assert model.model_id == "grok-4.6"
            assert "原任务及授权约束" in prompt
            assert set(policy.node_tools) == {"ReadFile", "WriteFile", "Glob", "Grep"}
            (tmp_path / "overview.md").write_bytes("完整正文\n".encode("utf-8"))
            return ExpertRoleResult(text="written", status="completed", handoff={"status": "completed",
                "artifacts": [{"path": "overview.md"}]})
        assert policy.stage_inputs["analyze"]["handoff"]["escalation_receipt"]["status"] == "completed"
        return ExpertRoleResult(text="verified", status="completed")

    orchestrator = ExpertGroupOrchestrator(runner=None, node_runner=runner, work_dir=tmp_path,
        archive_root=tmp_path / "archives", event_sink=events.append)
    profile = replace(_profile(), capability_executor=ModelRef("desktop:grok-main", "grok-4.6", "high"))
    result = await orchestrator.run("将分析写入 overview.md", profile, grok_catalog(), strategy="stable", flow=flow)
    assert result.status == "completed"
    assert calls == [("analyze", "manager"), ("analyze__file_delivery", "executor"), ("final", "manager")]
    assert [event["status"] for event in events if event.get("phase") == "capability_escalation"] == ["running", "completed"]


@pytest.mark.asyncio
async def test_orchestrator_does_not_invent_a_route_for_an_unconfigured_slot(tmp_path):
    from mewcode.expert_group import ExpertGroupOrchestrator
    from mewcode.experts.flow import ExpertFlowSpec
    from tests.test_expert_flow import _profile

    flow = ExpertFlowSpec.from_value({"flow_id": "no-grok", "label": "no fallback",
        "nodes": [{"node_id": "analyze", "role": "manager", "label": "analysis"},
                  {"node_id": "final", "role": "manager", "label": "final"}],
        "edges": [{"from": "analyze", "to": "final"}], "finalizer_node_id": "final"})
    calls, events = [], []

    async def runner(node, model, prompt, policy, member=None):
        calls.append((node.node_id, node.role, model.model_id))
        if node.role == "executor":
            raise AssertionError("profile executor must not be used as a fallback")
        if node.node_id == "analyze":
            return ExpertRoleResult(text="request", status="needs_attention",
                handoff=SubmitHandoffParams(summary="request file", status="blocked",
                    blocker_kind="capability_missing",
                    inline_outputs=[{"name": "overview", "content": "完整正文\n"}],
                    file_requests=[{"output_name": "overview", "path": "overview.md"}]).model_dump())
        return ExpertRoleResult(text="final", status="completed")

    orchestrator = ExpertGroupOrchestrator(runner=None, node_runner=runner, work_dir=tmp_path,
        archive_root=tmp_path / "archives", event_sink=events.append)
    result = await orchestrator.run("将分析写入 overview.md", _profile(), grok_catalog(), strategy="stable", flow=flow)
    assert result.status != "completed"
    assert not any(role == "executor" for _, role, _ in calls)
    blocked = [event for event in events if event.get("phase") == "capability_escalation"]
    assert blocked[-1]["executor_model"] == ""
    assert blocked[-1]["executor_provider"] == ""
    assert blocked[-1]["fallback"] is False
    assert "尚未配置辅助执行器" in blocked[-1]["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [
    ModelRef("cc-switch:claude", "claude-legacy-test", "medium"),
    ModelRef("codex:custom", "gpt-6.1-sol", "medium"),
    ModelRef("desktop:grok-main", "grok-4.6", "high"),
])
async def test_orchestrator_uses_explicit_capability_executor_for_command_probe(tmp_path, selected):
    from mewcode.expert_group import ExpertGroupOrchestrator
    from mewcode.experts.flow import ExpertFlowSpec
    from tests.test_expert_flow import _profile

    command = "python --version"
    flow = ExpertFlowSpec.from_value({"flow_id": "command-escalation", "label": "probe",
        "nodes": [{"node_id": "probe", "role": "planner", "label": "probe"},
                  {"node_id": "final", "role": "manager", "label": "final"}],
        "edges": [{"from": "probe", "to": "final"}], "finalizer_node_id": "final"})
    calls, events = [], []

    async def runner(node, model, prompt, policy, member=None):
        calls.append((node.node_id, node.role))
        if node.node_id == "probe__command_probe":
            assert model == selected
            assert policy.command_allowlist == (command,)
            return ExpertRoleResult(text="Python 3.12.8", status="completed",
                handoff={"status": "completed", "summary": "probe complete",
                         "evidence": [{"kind": "command", "ref": command, "result": "Python 3.12.8"}]},
                verification_records=[{"name": "Bash", "args": {"command": command},
                                       "status": "completed", "is_error": False,
                                       "output": "Python 3.12.8\nEXIT CODE: 0"}])
        if node.node_id == "probe" and calls.count(("probe", "planner")) == 1:
            return ExpertRoleResult(text="request probe", status="needs_attention",
                handoff=SubmitHandoffParams(summary="probe", status="blocked",
                    blocker_kind="capability_missing", blockers=["missing Bash"],
                    command_requests=[command_request(command)]).model_dump())
        if node.node_id == "probe":
            assert "probe__command_probe" in policy.stage_inputs
            assert policy.stage_inputs["probe__command_probe"]["handoff"]["evidence"][0]["result"] == "Python 3.12.8"
            return ExpertRoleResult(text="environment verified", status="completed")
        return ExpertRoleResult(text="final", status="completed")

    orchestrator = ExpertGroupOrchestrator(runner=None, node_runner=runner, work_dir=tmp_path,
        archive_root=tmp_path / "archives", event_sink=events.append)
    selected_profile = replace(_profile(), capability_executor=selected)
    catalog = grok_catalog()
    catalog["providers"].append({
        "key": selected.provider_key, "name": "Chosen helper",
        "models": [selected.model_id],
    })
    result = await orchestrator.run("只读探测 Python 环境", selected_profile, catalog,
        strategy="stable", flow=flow)
    assert result.status == "completed"
    assert calls == [("probe", "planner"), ("probe__command_probe", "executor"),
                     ("probe", "planner"), ("final", "manager")]
    assert any(event.get("phase") == "capability_followup" for event in events)
